"""prompt_toolkit-powered REPL for the Prometheus launcher.

Thin presentation layer over the pure-logic Dispatcher (commands.py).
Owns only what the Dispatcher cannot: prompt_toolkit I/O, log-file
tailing, and atomic config persistence.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import deque

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory

from src.launcher.commands import (
    CommandParser,
    InvalidTargetError,
    MissingArgumentError,
    UnknownCommandError,
)

LOG_PATHS = {
    "qq": "log/launcher/qq.log",
    "scraper": "log/web_scraper/scraper.log",
    "viewer": "log/viewer/viewer.log",
    "prometheus": "log/prometheus/prometheus.log",
    "tui": "log/launcher/tui.log",
}

COMPLETER_WORDS = [
    "start", "stop", "restart", "status", "logs", "config", "health",
    "tail", "help", "quit", "clear",
    "qq", "tui", "viewer", "scraper",
    "show", "set", "feeds", "stats",
]

_TAIL_POLL_INTERVAL = 1.0
_FEEDS_TAIL_COUNT = 10


class Shell:
    def __init__(self, pm, config, config_path, dispatcher):
        self.pm = pm
        self.config = config
        self.config_path = config_path
        self.dispatcher = dispatcher
        self.parser = CommandParser()
        self._session = None

    def run(self):
        completer = WordCompleter(COMPLETER_WORDS, ignore_case=True)
        session = PromptSession(
            history=InMemoryHistory(),
            completer=completer,
            bottom_toolbar=self._bottom_toolbar,
        )
        self._session = session

        print("=== Prometheus Launcher ===")
        print("Type 'help' for available commands.\n")

        while True:
            try:
                user_input = session.prompt("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                self._shutdown()
                break

            try:
                cmd = self.parser.parse(user_input)
            except (UnknownCommandError, MissingArgumentError, InvalidTargetError) as e:
                print(str(e))
                continue

            # The Shell owns config persistence: parse the value as JSON
            # and write atomically, which the Dispatcher cannot do (it
            # only stores a raw string in the in-memory dict).
            if cmd.verb == "config" and cmd.noun == "set":
                self._handle_config_set(cmd.args[0], cmd.args[1])
                continue

            result = self.dispatcher.dispatch(cmd)

            # TUI is a full-screen textual app that takes over the terminal.
            # The Shell must yield the terminal and block until TUI exits,
            # then pop the process so the monitor thread doesn't auto-restart
            # what the user intentionally quit.
            if result["ok"] and cmd.verb in ("start", "restart") and cmd.noun == "tui":
                tui_proc = self.pm.processes.get("tui")
                if tui_proc and tui_proc.poll() is None:
                    try:
                        tui_proc.wait()
                    except KeyboardInterrupt:
                        pass
                    self.pm.processes.pop("tui", None)
                    self._restore_terminal()

            action = result.get("data", {}).get("action")

            if action == "quit":
                self._shutdown()
                break
            elif action == "clear":
                print("\033[2J\033[H", end="")
            elif action == "tail_log":
                self._tail_log(result["data"]["target"])
            elif action == "tail_feeds":
                self._handle_tail_feeds()
            elif action == "tail_stats":
                self._handle_tail_stats()
            elif result["message"]:
                print(result["message"])

    def _bottom_toolbar(self):
        try:
            s = self.pm.get_status()
        except Exception:
            return "Status unavailable"
        return "QQ: {qq} | Scraper: {scraper} | TUI: {tui} | Viewer: {viewer}".format(
            qq=s.get("qq", "?"), scraper=s.get("scraper", "?"),
            tui=s.get("tui", "?"), viewer=s.get("viewer", "?"),
        )

    def _tail_log(self, target):
        path = LOG_PATHS.get(target)
        if path is None:
            print("No log file configured for '{0}'.".format(target))
            return
        if not os.path.exists(path):
            print("Log file not found: {0}".format(path))
            return

        print("Tailing {0} (Ctrl+C to stop)...".format(path))
        try:
            with open(path, "r") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        print(line, end="", flush=True)
                    else:
                        time.sleep(_TAIL_POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n[tail stopped]")

    def _handle_config_set(self, key, value):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            parsed = value

        self.config[key] = parsed

        # Atomic write: temp file must be in the same directory as the
        # target so os.replace is an atomic rename on the same filesystem.
        try:
            dir_path = os.path.dirname(self.config_path) or "."
            fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self.config, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.config_path)
            except OSError:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise
            print("config updated: {0} = {1}".format(key, parsed))
        except OSError as e:
            print(
                "Warning: config updated in memory but failed to persist: {0}".format(e)
            )

    def _handle_tail_feeds(self):
        path = os.path.join("data", "feeds.jsonl")
        if not os.path.exists(path):
            print("No feeds file found: {0}".format(path))
            return

        # deque(f, maxlen=N) streams the whole file but only keeps the
        # last N lines — constant memory even for multi-GB archives.
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = deque(f, maxlen=_FEEDS_TAIL_COUNT)
        except OSError as e:
            print("Error reading feeds file: {0}".format(e))
            return

        if not lines:
            print("Feed archive is empty.")
            return

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                feed = entry.get("feed", {}) if isinstance(entry, dict) else {}
                print("  [{0}] {1}".format(
                    feed.get("id", "?"), feed.get("title", "(no title)")
                ))
            except (json.JSONDecodeError, KeyError, AttributeError):
                print("  {0}".format(line[:80]))

    def _handle_tail_stats(self):
        path = os.path.join("data", "feeds.jsonl")
        if not os.path.exists(path):
            print("No feeds file found: {0}".format(path))
            return

        try:
            size = os.path.getsize(path)
            count = 0
            with open(path, "r", encoding="utf-8") as f:
                for _ in f:
                    count += 1
        except OSError as e:
            print("Error reading feeds file: {0}".format(e))
            return

        print("Archive stats:")
        print("  Total feeds: {0}".format(count))
        print("  File size: {0:.1f} MB".format(size / (1024 * 1024)))

    def _restore_terminal(self):
        try:
            subprocess.run(["reset"], timeout=3)
        except Exception:
            pass
        try:
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass

    def _shutdown(self):
        print("Shutting down...")
        try:
            self.pm.graceful_shutdown()
        except Exception:
            pass
