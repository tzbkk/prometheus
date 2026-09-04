"""ProcessManager: 三目标进程监督（scraper/deepbackfill/viewer）。

纯监督者。archive 不在监督面（打包是批处理，走 scripts/archive.py CLI，
不是常驻服务）。本模块只做三件事：

1. 目标生命周期：start/stop/restart + 崩溃自愈（monitor + restarts 预算）。
2. 富状态对象：TargetStatus 契约形——
   ``{"name", "state": stopped|running|failed, "pid", "uptime_sec", "restarts"}``；
   stopped/failed 态 pid 与 uptime 为 null（无活进程可指）。
3. 日志定位：每目标日志路径（start 时追加写）。

启动命令 = 各组件契约 Runs 字段（components/*.yaml）：
  scraper      python -m src.web_scraper
  viewer       python -m src.viewer.backend.server --port <viewer_port>
  deepbackfill python -m src.deepbackfill --port <deepbackfill_port>

线程安全：API 线程（ThreadingHTTPServer）、REPL 线程与 monitor 线程并发
调用，全部公开方法经 ``self._lock``（RLock——restart 内嵌 stop+start）串行。
"""

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

_PR_SET_PDEATHSIG = 1

TARGETS = ("scraper", "deepbackfill", "viewer")

_STOP_TIMEOUTS = {
    "scraper": 1,
    "deepbackfill": 5,
    "viewer": 5,
}

_LOG_PATHS = {
    "scraper": os.path.join("log", "web_scraper", "scraper.log"),
    "deepbackfill": os.path.join("log", "deepbackfill", "deepbackfill.log"),
    "viewer": os.path.join("log", "viewer", "viewer.log"),
}

_LABELS = {
    "scraper": "Scraper",
    "deepbackfill": "Deepbackfill",
    "viewer": "Viewer",
}

_DEFAULT_VIEWER_PORT = 9422
_DEFAULT_DEEPBACKFILL_PORT = 9424
_DEFAULT_SCRAPER_PORT = 9420
_DEFAULT_MAX_RESTARTS = 5
_LOCK_CONFLICT_EXIT = 2


def _set_pdeathsig():
    libc = ctypes.CDLL("libc.so.6")
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


class ProcessManager:
    def __init__(self, config):
        self.config = config
        self.processes = {}
        self.restart_counts = {name: 0 for name in TARGETS}
        self.project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self._lock = threading.RLock()

    # ---- 启动命令（契约 Runs 字段逐字）----

    def _command(self, name):
        if name == "scraper":
            return [sys.executable, "-m", "src.web_scraper"]
        if name == "viewer":
            port = str(self.config.get("viewer_port", _DEFAULT_VIEWER_PORT))
            return [
                sys.executable, "-m", "src.viewer.backend.server", "--port", port,
            ]
        if name == "deepbackfill":
            port = str(
                self.config.get("deepbackfill_port", _DEFAULT_DEEPBACKFILL_PORT)
            )
            return [sys.executable, "-m", "src.deepbackfill", "--port", port]
        raise ValueError("Unknown supervision target: {0}".format(name))

    def log_path(self, name):
        """目标日志文件绝对路径（start 追加写；API LogTail / shell tail 共用）。"""
        return os.path.join(self.project_root, _LOG_PATHS[name])

    # ---- 生命周期 ----

    def start(self, name):
        """启动目标（新生命周期——restarts 归零）。已运行则原样保留。"""
        with self._lock:
            entry = self.processes.get(name)
            if entry is not None and entry["proc"].poll() is None:
                return
            args = self._command(name)
            env = dict(os.environ)
            kwargs = {
                "cwd": self.project_root,
                "env": env,
                "preexec_fn": _set_pdeathsig,
            }
            path = self.log_path(name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            logfile = open(path, "a")
            kwargs.update(
                stdout=logfile,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            self.processes[name] = {
                "proc": subprocess.Popen(args, **kwargs),
                "started_at": time.time(),
                "logfile": logfile,
            }
            self.restart_counts[name] = 0

    def stop(self, name):
        """停止目标（幂等——无在管进程即 no-op）。"""
        with self._lock:
            self._stop_locked(name)

    def _stop_locked(self, name):
        entry = self.processes.pop(name, None)
        if entry is None:
            return
        proc = entry["proc"]
        proc.terminate()
        try:
            proc.wait(timeout=_STOP_TIMEOUTS[name])
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if entry["logfile"] is not None:
            try:
                entry["logfile"].close()
            except OSError:
                pass

    def restart(self, name):
        """restart = stop + start（返回前新进程已在管；restarts 计一次）。"""
        with self._lock:
            self._stop_locked(name)
            self.start(name)
            self.restart_counts[name] += 1

    # ---- 状态面（TargetStatus 契约形）----

    def status_of(self, name):
        """单目标富状态对象（TargetStatus 同构——TargetList 条目）。"""
        with self._lock:
            restarts = self.restart_counts.get(name, 0)
            entry = self.processes.get(name)
            if entry is None:
                return {
                    "name": name,
                    "state": "stopped",
                    "pid": None,
                    "uptime_sec": None,
                    "restarts": restarts,
                }
            if entry["proc"].poll() is None:
                return {
                    "name": name,
                    "state": "running",
                    "pid": entry["proc"].pid,
                    "uptime_sec": int(time.time() - entry["started_at"]),
                    "restarts": restarts,
                }
            # 在管但已退出且未被 stop 清账：崩溃残留 → failed（真实可观测，
            # monitor 周期内自愈；预算耗尽则驻留此态）。
            return {
                "name": name,
                "state": "failed",
                "pid": None,
                "uptime_sec": None,
                "restarts": restarts,
            }

    def status_all(self):
        """三目标快照（TargetList 形——一次返回全部目标）。"""
        with self._lock:
            return {
                "targets": [self.status_of(name) for name in TARGETS]
            }

    # ---- 自愈监督 ----

    def can_restart(self, name):
        max_restarts = self.config.get("max_restarts", _DEFAULT_MAX_RESTARTS)
        return self.restart_counts[name] < max_restarts

    def auto_restart(self, name):
        if not self.can_restart(name):
            sys.stderr.write(
                "[ERROR] max_restarts exceeded for {0}; not restarting\n".format(name)
            )
            return False
        self.restart(name)
        return True

    def monitor(self):
        with self._lock:
            for name in TARGETS:
                entry = self.processes.get(name)
                if entry is None:
                    continue
                rc = entry["proc"].poll()
                if rc is None:
                    continue
                if rc == _LOCK_CONFLICT_EXIT:
                    # 锁冲突退出（scraper 家族 exit 2）——不重启。
                    self._stop_locked(name)
                    continue
                self.auto_restart(name)

    def graceful_shutdown(self):
        """依序优雅关停全部目标（ShutdownAck 语义——各目标依序退出）。"""
        with self._lock:
            for name in TARGETS:
                self._stop_locked(name)

    # ---- 健康检查（scraper :9420——shell health 命令消费）----

    def wait_health_check(self, timeout=5):
        api_port = self.config.get(
            "scraper_api_port", _DEFAULT_SCRAPER_PORT
        )
        url = "http://127.0.0.1:{0}/health".format(api_port)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if getattr(resp, "status", resp.getcode()) == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def install_signal_handlers(self):
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, signum, frame):
        self.graceful_shutdown()
        sys.exit(0)
