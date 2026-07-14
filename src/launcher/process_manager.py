"""ProcessManager: subprocess lifecycle for QQ + TUI with auto-restart and graceful shutdown.

Uses only Python stdlib. QQ = bash start_qq.sh; TUI = python -m src.tui.
"""

import ctypes
import os
import signal
import subprocess
import sys
import time
import urllib.request

_PR_SET_PDEATHSIG = 1


def _set_pdeathsig():
    libc = ctypes.CDLL("libc.so.6")
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)

_QQ_STOP_TIMEOUT = 10
_TUI_STOP_TIMEOUT = 5
_VIEWER_STOP_TIMEOUT = 5
_HEALTH_POLL_INTERVAL = 1
_DEFAULT_API_PORT = 9420
_DEFAULT_LAUNCHER_PORT = 9421
_DEFAULT_VIEWER_PORT = 9422
_DEFAULT_MAX_RESTARTS = 5
_DEFAULT_QQ_SCRIPT = "scripts/start_qq.sh"


class ProcessManager:
    def __init__(self, config):
        self.config = config
        self.processes = {}
        self.restart_counts = {"qq": 0, "tui": 0, "viewer": 0}
        self.project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )

    def start_qq(self):
        script = self.config.get("qq_start_script", _DEFAULT_QQ_SCRIPT)
        logfile = open(os.path.join(self.project_root, "log", "launcher", "qq.log"), "a")
        self.processes["qq"] = subprocess.Popen(
            ["bash", script], cwd=self.project_root,
            stdout=logfile, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_set_pdeathsig,
        )

    def start_tui(self):
        port = str(self.config.get("launcher_port", _DEFAULT_LAUNCHER_PORT))
        env = dict(os.environ)
        env["TEXTUAL_DISABLE_KITTY_KEY"] = "1"
        self.processes["tui"] = subprocess.Popen(
            [sys.executable, "-m", "src.tui", "--port", port],
            cwd=self.project_root,
            env=env,
            preexec_fn=_set_pdeathsig,
        )

    def start_viewer(self):
        port = str(self.config.get("viewer_port", _DEFAULT_VIEWER_PORT))
        env = dict(os.environ)
        log_dir = os.path.join(self.project_root, "log", "viewer")
        os.makedirs(log_dir, exist_ok=True)
        log_file = open(os.path.join(log_dir, "viewer.log"), "a")
        self.processes["viewer"] = subprocess.Popen(
            [sys.executable, "-m", "src.viewer.backend.server", "--port", port],
            cwd=self.project_root,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=_set_pdeathsig,
        )

    def stop_qq(self):
        self._stop("qq", _QQ_STOP_TIMEOUT)

    def stop_tui(self):
        self._stop("tui", _TUI_STOP_TIMEOUT)

    def stop_viewer(self):
        self._stop("viewer", _VIEWER_STOP_TIMEOUT)

    def _stop(self, name, timeout):
        proc = self.processes.get(name)
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self.processes.pop(name, None)

    def restart_qq(self):
        start = time.time()
        self.stop_qq()
        self.start_qq()
        success = self.wait_health_check(timeout=30)
        elapsed_ms = int((time.time() - start) * 1000)
        return (success, elapsed_ms)

    def wait_health_check(self, timeout=30):
        api_port = self.config.get("api_port", _DEFAULT_API_PORT)
        url = "http://127.0.0.1:{0}/health".format(api_port)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if getattr(resp, "status", resp.getcode()) == 200:
                        return True
            except Exception:
                pass
            time.sleep(_HEALTH_POLL_INTERVAL)
        return False

    def can_restart(self, name):
        max_restarts = self.config.get("max_restarts", _DEFAULT_MAX_RESTARTS)
        return self.restart_counts[name] < max_restarts

    def auto_restart(self, name):
        if not self.can_restart(name):
            sys.stderr.write(
                "[ERROR] max_restarts exceeded for {0}; not restarting\n".format(name)
            )
            return False
        self.restart_counts[name] += 1
        if name == "qq":
            self.restart_qq()
        elif name == "tui":
            self.stop_tui()
            self.start_tui()
        elif name == "viewer":
            self.stop_viewer()
            self.start_viewer()
        return True

    def monitor(self):
        for name in ("qq", "tui", "viewer"):
            proc = self.processes.get(name)
            if proc is None:
                continue
            if proc.poll() is not None:
                self.processes.pop(name, None)
                self.auto_restart(name)

    def graceful_shutdown(self):
        self.stop_qq()
        self.stop_tui()
        self.stop_viewer()

    def get_status(self):
        qq_proc = self.processes.get("qq")
        return {
            "qq": self._proc_status("qq", allow_crashed=True),
            "tui": self._proc_status("tui", allow_crashed=False),
            "viewer": self._proc_status("viewer", allow_crashed=True),
            "restart_counts": dict(self.restart_counts),
            "qq_pid": qq_proc.pid if qq_proc else None,
        }

    def _proc_status(self, name, allow_crashed):
        proc = self.processes.get(name)
        if proc is None:
            return "stopped"
        if proc.poll() is None:
            return "running"
        return "crashed" if allow_crashed else "stopped"

    def install_signal_handlers(self):
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, signum, frame):
        self.graceful_shutdown()
        sys.exit(0)
