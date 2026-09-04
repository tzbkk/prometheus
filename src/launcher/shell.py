"""prompt_toolkit-powered REPL for the Prometheus launcher.

Thin presentation layer over the pure-logic Dispatcher (commands.py).
Owns only what the Dispatcher cannot: prompt_toolkit I/O, log-file
tailing, and atomic config persistence.

监督面在本 shell——start/stop/restart/
status/logs/config/health/tail 即全部监督动词。
"""

import json
import os
import signal
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory

from .commands import (
    CommandParser,
    InvalidTargetError,
    MissingArgumentError,
    UnknownCommandError,
)

COMPLETER_WORDS = [
    "start", "stop", "restart", "logs", "config", "health",
    "stats", "archive", "auth", "help", "quit", "shutdown", "clear",
    "scraper", "deepbackfill", "viewer",
    "show", "set",
    "--apply", "--force",
]

_ARCHIVE_DATA_DIR = "data"

_TAIL_POLL_INTERVAL = 1.0


class Shell:
    def __init__(self, pm, config, config_path, dispatcher):
        # pm：宿主模式 = ProcessManager；远程模式 = LauncherClient
        # （status_all/status_of/log_path 鸭子类型同形——零分支复用）。
        self.pm = pm
        self.config = config
        self.config_path = config_path
        self.dispatcher = dispatcher
        self.remote = getattr(dispatcher, "remote", None)
        self.parser = CommandParser()
        self._session = None

    def run(self):
        completer = WordCompleter(
            COMPLETER_WORDS + self._archive_guild_candidates(),
            ignore_case=True,
        )
        session = PromptSession(
            history=InMemoryHistory(),
            completer=completer,
            bottom_toolbar=self._bottom_toolbar,
        )
        self._session = session

        print("=== Prometheus Launcher ===")
        if self.remote:
            print("client shell — daemon :{0} (targets keep running after"
                  " quit; 'shutdown' stops all)".format(self.remote.port))
        print("Type 'help' for available commands.\n")

        while True:
            try:
                user_input = session.prompt("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                if self.remote:
                    self._exit_client()
                else:
                    self._shutdown()
                break

            try:
                cmd = self.parser.parse(user_input)
            except (UnknownCommandError, MissingArgumentError, InvalidTargetError) as e:
                print(str(e))
                continue

            try:
                result = self.dispatcher.dispatch(cmd)
            except Exception as e:  # noqa: BLE001 —— REPL 装甲：动词异常不炸壳
                print("error: {0}".format(e))
                continue

            action = result.get("data", {}).get("action")

            if action == "quit":
                if self.remote:
                    self._exit_client()
                else:
                    self._shutdown()
                break
            elif action == "shutdown_tree":
                self._shutdown_tree()
                break
            elif action == "clear":
                print("\033[2J\033[H", end="")
            elif action == "auth":
                self._handle_auth()
            elif action == "tail_log":
                self._tail_log(result["data"]["target"])
            elif result["message"]:
                print(result["message"])

            # 纯网登录：start deepbackfill 成功后自动探测凭证——缺/失效
            # 则开浏览器扫码并等待（ok 立即返回，全程可 Ctrl+C 安全回提示符）。
            if result["ok"] and cmd.verb == "start" and cmd.noun == "deepbackfill":
                self._wait_deepbackfill_auth()

    def _bottom_toolbar(self):
        try:
            s = self.pm.status_all()
        except Exception:
            return "Status unavailable"
        labels = {
            "scraper": "Scraper",
            "deepbackfill": "Deepbackfill",
            "viewer": "Viewer",
        }
        return " | ".join(
            "{0}: {1}".format(labels[t["name"]], t["state"]) for t in s["targets"]
        )

    @staticmethod
    def _archive_guild_candidates():
        """data/ 下数字 guild 目录名（含 feeds/ 或 comments/ 子树）→ Tab 候选。

        与引擎 list_guilds 同一过滤形，但纯 os 路径判定——不 import 引擎
        （zhizong 依赖不入补全面），表现层自持。
        """
        try:
            entries = os.listdir(_ARCHIVE_DATA_DIR)
        except OSError:
            return []
        guilds = []
        for name in entries:
            root = os.path.join(_ARCHIVE_DATA_DIR, name)
            if name.isdigit() and (
                os.path.isdir(os.path.join(root, "feeds"))
                or os.path.isdir(os.path.join(root, "comments"))
            ):
                guilds.append(name)
        return sorted(guilds)

    def _handle_auth(self):
        """``auth`` 动词——网页扫码登录流（薄版：服务面出码）。

        deepbackfill 服务在跑 → 同 start 流（开浏览器 + 等 ok）；未跑 →
        可读提示先 start deepbackfill。任步失败打印可读错误回提示符。
        """
        if self.pm.status_of("deepbackfill")["state"] != "running":
            print(
                "deepbackfill 服务未运行——先 start deepbackfill"
                "（启动后自动探测凭证并弹出扫码浏览器）"
            )
            return
        self._run_web_login_wait()

    def _wait_deepbackfill_auth(self):
        self._run_web_login_wait()

    def _run_web_login_wait(self):
        from src.launcher.auth_flow import wait_for_web_login

        port = self.config.get("deepbackfill_port", 9424)
        try:
            wait_for_web_login(port)
        except Exception as e:  # noqa: BLE001 —— REPL 兜底：等待流异常不炸壳
            print("auth wait failed: {0}".format(e))

    def _tail_log(self, target):
        path = self.pm.log_path(target)
        if not os.path.exists(path):
            print("Log file not found: {0}".format(path))
            return

        print("Tailing {0} (Ctrl+C to stop)...".format(path))
        # __main__'s process-wide SIGINT handler exits the whole launcher;
        # during tailing, Ctrl+C must instead surface as KeyboardInterrupt
        # (caught below). Restore the original handler unconditionally —
        # prompt-loop Ctrl+C semantics stay untouched.
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
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
        finally:
            signal.signal(signal.SIGINT, previous)


    def _shutdown(self):
        print("Shutting down...")
        try:
            self.pm.graceful_shutdown()
        except Exception:
            pass

    def _exit_client(self):
        print(
            "client shell closed — targets keep running"
            " (daemon :{0}; 'shutdown' verb stops all)".format(
                self.remote.port if self.remote else "?"
            )
        )

    def _shutdown_tree(self):
        if self.remote:
            print("Stopping daemon and all targets...")
            try:
                self.remote.shutdown()
            except Exception as e:  # noqa: BLE001 —— REPL 兜底：远程关停失败不炸壳
                print("remote shutdown failed: {0}".format(e))
            print("Tree stopped (daemon :{0}).".format(self.remote.port))
        else:
            self._shutdown()

    def _handle_config_set_remote(self, key, raw_value):
        """远程 config set：解析 JSON 值 → PUT /config（守护进程单写者）。"""
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        try:
            merged = self.remote.config_set(key, value)
        except Exception as e:  # noqa: BLE001 —— REPL 兜底：PUT 失败不炸壳
            print("config set failed: {0}".format(e))
            return
        self.config = merged
        print("config updated: {0} = {1}".format(
            key, json.dumps(value, ensure_ascii=False)
        ))
