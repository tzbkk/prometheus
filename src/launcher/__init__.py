"""Prometheus launcher: process manager for QQ + TUI with auto-restart."""

import json
import os
import signal
import sys
import time

from .process_manager import ProcessManager

__all__ = ["ProcessManager", "main"]

CONFIG_PATH = os.path.join("conf", "launcher.conf.json")
MONITOR_INTERVAL = 2


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    config = _load_config()
    os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    pm = ProcessManager(config)
    pm.install_signal_handlers()
    pm.start_qq()
    if config.get("start_tui", True):
        pm.start_tui()

    restart_delay = config.get("restart_delay", 5)
    while True:
        time.sleep(MONITOR_INTERVAL)
        pm.monitor()
        if any(pm.restart_counts.values()) and restart_delay:
            time.sleep(restart_delay)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    main()
