"""Process-level lock with stale-PID detection and crash recovery."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class LockError(Exception):
    pass


class Lock:
    LOCK_FILE = "prometheus.lock"

    def __init__(self, data_dir: Path, logger=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.data_dir / self.LOCK_FILE
        self.logger = logger
        self._held = False

    def _read(self) -> dict | None:
        if not self.lock_path.exists():
            return None
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, state: dict) -> None:
        tmp = self.lock_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.lock_path)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

    def acquire(self, cycle: int = 0) -> dict:
        old = self._read()
        if old and old.get("pid") and old.get("dirty"):
            pid = old["pid"]
            if self._pid_alive(pid):
                raise LockError(
                    f"another instance running (pid={pid})"
                )
            if self.logger:
                self.logger.warning(
                    "stale lock from dead pid=%s, overwriting", pid
                )

        state = {
            "pid": os.getpid(),
            "dirty": True,
            "cycle": cycle,
            "ts": int(time.time()),
        }
        self._write(state)
        self._held = True
        return state

    def release(self) -> None:
        if not self._held:
            return
        state = self._read()
        if state:
            state["dirty"] = False
            self._write(state)
        self._held = False

    def check_and_recover(self) -> dict | None:
        old = self._read()
        if not old or not old.get("dirty"):
            return None
        pid = old.get("pid", 0)
        if pid and self._pid_alive(pid):
            return None
        return {
            "crashed": True,
            "cycle": old.get("cycle"),
            "ts": old.get("ts"),
        }
