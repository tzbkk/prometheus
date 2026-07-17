"""Command parser and dispatcher for the launcher shell.

Pure logic layer (no prompt_toolkit). Two components:

1. CommandParser — tokenize user input into a ``Cmd`` dataclass.
2. Dispatcher — execute a ``Cmd`` against a ``ProcessManager``, returning a
   response envelope ``{"ok": bool, "message": str, "data": dict}``.

The Dispatcher is thread-safe: all ``ProcessManager`` calls are serialized via
an internal ``threading.Lock`` because ``ProcessManager`` itself is not
thread-safe and may be invoked concurrently by the REPL thread and a
background monitor thread.
"""

import json
import threading
from dataclasses import dataclass, field

HELP_TEXT = """Available commands:
  start <qq|tui|viewer|scraper>   Start a process
  stop <qq|tui|viewer|scraper>    Stop a process
  restart <qq|tui|viewer|scraper> Restart a process
  status                  Show process status
  logs <qq|tui|viewer>    Tail process log (Ctrl+C to stop)
  config show             Show current config
  config set <key> <val>  Update config value
  health                  Check QQ API health
  tail feeds              Show last 10 archived feeds
  tail stats              Show archive statistics
  help                    Show this help
  quit                    Shut down all processes and exit
  clear                   Clear the screen"""


@dataclass
class Cmd:
    """A parsed command: verb (action), noun (target/subcommand), args."""

    verb: str
    noun: str = ""
    args: list = field(default_factory=list)


class UnknownCommandError(Exception):
    """Raised when the verb is not recognised."""


class MissingArgumentError(Exception):
    """Raised when a verb requires a noun/subcommand but none was given."""


class InvalidTargetError(Exception):
    """Raised when a noun is not a valid target."""


class CommandParser:
    """Tokenize user input strings into ``Cmd`` objects.

    Grammar::

        verb:    start | stop | restart | status | logs | config | health |
                 tail | help | quit | clear
        target:  qq | tui | viewer
        config:  config show | config set <key> <value...>
        tail:    tail feeds | tail stats
        logs:    logs <target>
    """

    VALID_VERBS = {
        "start", "stop", "restart", "status", "logs",
        "config", "health", "tail", "help", "quit", "clear",
    }
    VALID_TARGETS = {"qq", "tui", "viewer", "scraper"}
    VERBS_REQUIRING_TARGET = {"start", "stop", "restart", "logs"}
    _TAIL_NOUNS = ("feeds", "stats")

    def parse(self, text):
        """Parse ``text`` into a :class:`Cmd`.

        Raises :class:`UnknownCommandError`, :class:`MissingArgumentError`,
        or :class:`InvalidTargetError` on invalid input.
        """
        text = text.strip().lower()
        if not text:
            return Cmd(verb="noop")

        tokens = text.split()
        verb = tokens[0]

        if verb not in self.VALID_VERBS:
            raise UnknownCommandError(
                "Unknown command: '{0}'. Type 'help' for available commands.".format(verb)
            )

        if verb in self.VERBS_REQUIRING_TARGET:
            return self._parse_targeted(verb, tokens)

        if verb == "config":
            return self._parse_config(tokens)

        if verb == "tail":
            return self._parse_tail(tokens)

        return Cmd(verb=verb)

    def _parse_targeted(self, verb, tokens):
        if len(tokens) < 2:
            raise MissingArgumentError(
                "'{0}' requires a target: qq, tui, viewer, or scraper.".format(verb)
            )
        noun = tokens[1]
        if noun not in self.VALID_TARGETS:
            raise InvalidTargetError(
                "Invalid target: '{0}'. Must be one of: qq, tui, viewer, scraper.".format(noun)
            )
        return Cmd(verb=verb, noun=noun, args=tokens[2:])

    def _parse_config(self, tokens):
        if len(tokens) < 2:
            raise MissingArgumentError(
                "'config' requires a subcommand: show or set."
            )
        sub = tokens[1]
        if sub == "show":
            return Cmd(verb="config", noun="show", args=[])
        if sub == "set":
            if len(tokens) < 4:
                raise MissingArgumentError(
                    "'config set' requires <key> <value>."
                )
            key = tokens[2]
            value = " ".join(tokens[3:])
            return Cmd(verb="config", noun="set", args=[key, value])
        raise MissingArgumentError(
            "'config' requires a subcommand: show or set."
        )

    def _parse_tail(self, tokens):
        if len(tokens) < 2:
            raise MissingArgumentError(
                "'tail' requires 'feeds' or 'stats'."
            )
        noun = tokens[1]
        if noun not in self._TAIL_NOUNS:
            raise InvalidTargetError("tail requires 'feeds' or 'stats'.")
        return Cmd(verb="tail", noun=noun, args=[])


START_METHODS = {
    "qq": "start_qq",
    "tui": "start_tui",
    "viewer": "start_viewer",
    "scraper": "start_scraper",
}
STOP_METHODS = {
    "qq": "stop_qq",
    "tui": "stop_tui",
    "viewer": "stop_viewer",
    "scraper": "stop_scraper",
}


class Dispatcher:
    """Execute a :class:`Cmd` against a ``ProcessManager``.

    All ``ProcessManager`` calls are serialized with an internal lock so the
    Dispatcher is safe to call from multiple threads (REPL + monitor).
    """

    def __init__(self, pm, config, config_path):
        self.pm = pm
        self.config = config
        self.config_path = config_path
        self._lock = threading.Lock()

    def dispatch(self, cmd):
        """Execute ``cmd`` and return ``{"ok": bool, "message": str, "data": dict}``."""
        with self._lock:
            return self._dispatch_locked(cmd)

    def _dispatch_locked(self, cmd):
        verb = cmd.verb
        if verb == "noop":
            return {"ok": True, "message": "", "data": {}}
        if verb == "help":
            return {"ok": True, "message": HELP_TEXT, "data": {}}
        if verb == "quit":
            return {"ok": True, "message": "Shutting down...",
                    "data": {"action": "quit"}}
        if verb == "clear":
            return {"ok": True, "message": "", "data": {"action": "clear"}}
        if verb == "status":
            s = self.pm.get_status()
            return {"ok": True, "message": self._format_status_string(s), "data": s}
        if verb == "health":
            ok = self.pm.wait_health_check(timeout=5)
            return {"ok": ok,
                    "message": "QQ health: OK" if ok else "QQ health: TIMEOUT",
                    "data": {}}
        if verb == "logs":
            return {"ok": True, "message": "",
                    "data": {"action": "tail_log", "target": cmd.noun}}
        if verb == "tail":
            return {"ok": True, "message": "",
                    "data": {"action": "tail_" + cmd.noun}}
        if verb == "config":
            return self._handle_config(cmd)
        if verb == "start":
            return self._handle_start(cmd.noun)
        if verb == "stop":
            return self._handle_stop(cmd.noun)
        if verb == "restart":
            return self._handle_restart(cmd.noun)
        # Parser is the gatekeeper; an unknown verb here is a programming error.
        raise ValueError("Unhandled command verb: {0}".format(verb))

    def _handle_start(self, noun):
        status = self.pm.get_status()
        if status[noun] == "running":
            return {"ok": False,
                    "message": "{0} is already running.".format(noun),
                    "data": {}}
        # Port 9420 mutual exclusion: scraper and qq cannot run simultaneously.
        if noun == "scraper" and status.get("qq") == "running":
            return {"ok": False,
                    "message": "Port 9420 is occupied by QQ. Stop QQ first.",
                    "data": {}}
        if noun == "qq" and status.get("scraper") == "running":
            return {"ok": False,
                    "message": "Port 9420 is occupied by scraper. Stop scraper first.",
                    "data": {}}
        getattr(self.pm, START_METHODS[noun])()
        self.pm.restart_counts[noun] = 0
        return {"ok": True, "message": "{0} started.".format(noun), "data": {}}

    def _handle_stop(self, noun):
        status = self.pm.get_status()
        if status[noun] == "stopped":
            return {"ok": False,
                    "message": "{0} is already stopped.".format(noun),
                    "data": {}}
        getattr(self.pm, STOP_METHODS[noun])()
        return {"ok": True, "message": "{0} stopped.".format(noun), "data": {}}

    def _handle_restart(self, noun):
        if noun == "qq":
            success, elapsed_ms = self.pm.restart_qq()
            self.pm.restart_counts[noun] = 0
            return {
                "ok": success,
                "message": "qq restarted (health: {0}).".format(
                    "OK" if success else "TIMEOUT"
                ),
                "data": {"health_check_ms": elapsed_ms},
            }
        # tui / viewer: stop then start
        getattr(self.pm, STOP_METHODS[noun])()
        getattr(self.pm, START_METHODS[noun])()
        self.pm.restart_counts[noun] = 0
        return {"ok": True, "message": "{0} restarted.".format(noun), "data": {}}

    def _handle_config(self, cmd):
        if cmd.noun == "show":
            return {"ok": True,
                    "message": json.dumps(self.config, indent=2, ensure_ascii=False),
                    "data": {}}
        key, value = cmd.args[0], cmd.args[1]
        self.config[key] = value
        return {"ok": True,
                "message": "config updated: {0} = {1}".format(key, value),
                "data": {}}

    @staticmethod
    def _format_status_string(s):
        """Render a status dict (from ``pm.get_status()``) as a multi-line string."""
        lines = [
            "  QQ:       {0:10s}  restart #{1}".format(
                s["qq"], s["restart_counts"]["qq"]
            ),
            "  Scraper:  {0:10s}  restart #{1}".format(
                s["scraper"], s["restart_counts"]["scraper"]
            ),
            "  TUI:      {0:10s}  restart #{1}".format(
                s["tui"], s["restart_counts"]["tui"]
            ),
            "  Viewer:   {0:10s}  restart #{1}".format(
                s["viewer"], s["restart_counts"]["viewer"]
            ),
        ]
        return "\n".join(lines)
