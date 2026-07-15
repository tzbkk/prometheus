"""Entry point: ``python3 -m src.launcher`` starts the LauncherApi server, a
background process monitor, and the interactive prompt_toolkit shell."""

import json
import os
import signal
import sys
import threading

from .api import LauncherApi
from .process_manager import ProcessManager

CONFIG_PATH = os.path.join("conf", "launcher.conf.json")


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    config = _load_config()
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.chdir(project_root)

    err_log = os.path.join("log", "launcher", "stderr.log")
    os.makedirs(os.path.dirname(err_log), exist_ok=True)
    sys.stderr = open(err_log, "a")

    pm = ProcessManager(config)

    api = LauncherApi(pm, port=config.get("launcher_port", 9421))
    api_thread = threading.Thread(target=api.serve_forever, daemon=True)
    api_thread.start()

    # Background monitor thread: checks for crashed processes and auto-restarts
    monitor_stop = threading.Event()

    def _monitor_loop():
        while not monitor_stop.is_set():
            try:
                pm.monitor()
            except Exception:
                pass  # Don't let monitor thread crash the launcher
            monitor_stop.wait(1.0)

    monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
    monitor_thread.start()

    def _on_signal(signum, frame):
        monitor_stop.set()
        pm.graceful_shutdown()
        api.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # shell.run() blocks until the quit command, then returns
    from .commands import Dispatcher
    from .shell import Shell

    dispatcher = Dispatcher(pm, config, CONFIG_PATH)
    shell = Shell(pm, config, CONFIG_PATH, dispatcher)
    shell.run()

    # After shell exits (quit command), clean up
    monitor_stop.set()
    pm.graceful_shutdown()
    api.stop()


if __name__ == "__main__":
    main()
