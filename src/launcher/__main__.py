"""Entry point: `python3 -m src.launcher` starts QQ + TUI + LauncherApi server."""

import json
import os
import signal
import subprocess
import sys
import threading

from .api import LauncherApi
from .process_manager import ProcessManager

CONFIG_PATH = os.path.join("conf", "launcher.conf.json")


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _print_status(pm):
    s = pm.get_status()
    print(f"  QQ:  {s['qq']:10s}  restart #{s['restart_counts']['qq']}")
    print(f"  TUI: {s['tui']:10s}  restart #{s['restart_counts']['tui']}")


def _restore_terminal():
    try:
        subprocess.run(["reset"], timeout=3)
    except Exception:
        pass
    try:
        import termios
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _wait_for_tui(pm):
    while True:
        proc = pm.processes.get("tui")
        if proc is None:
            return
        rc = proc.wait()
        _restore_terminal()
        if rc == 0:
            print("\nTUI exited cleanly")
            return
        if pm.can_restart("tui"):
            pm.restart_counts["tui"] += 1
            pm.start_tui()
            print("\nTUI crashed — restarted")
        else:
            print("\nTUI crashed — max restarts exceeded")
            return


def main():
    config = _load_config()
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.chdir(project_root)

    err_log = os.path.join("log", "launcher", "stderr.log")
    sys.stderr = open(err_log, "a")

    pm = ProcessManager(config)

    api = LauncherApi(pm, port=config.get("launcher_port", 9421))
    api_thread = threading.Thread(target=api.serve_forever, daemon=True)
    api_thread.start()

    def _on_signal(signum, frame):
        pm.graceful_shutdown()
        api.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    _wait_for_tui(pm)

    print("\n=== Prometheus Launcher ===")
    print("  [Enter]=status  s=start TUI  p=stop QQ  r=start all  q=quit  h=help\n")
    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            cmd = "q"
        if cmd in ("q", "quit", "exit"):
            print("Shutting down...")
            _on_signal(None, None)
            break
        elif cmd == "":
            _print_status(pm)
        elif cmd in ("s", "start"):
            pm.start_tui()
            _wait_for_tui(pm)
            print()
        elif cmd in ("p", "stop"):
            pm.stop_qq()
            print("QQ stopped")
        elif cmd in ("r", "restart"):
            pm.stop_qq()
            pm.start_qq()
            pm.start_tui()
            _wait_for_tui(pm)
            print()
        elif cmd in ("a", "status"):
            _print_status(pm)
        elif cmd in ("h", "help"):
            print("  [Enter]=status  s=start TUI  p=stop QQ  r=start all  q=quit  h=help")


if __name__ == "__main__":
    main()
