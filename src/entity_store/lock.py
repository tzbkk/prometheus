"""瞬态进程锁读写（契约唯一规格：structures/ProcessLock.yaml，5 字段全钉）。

契约要点（5 字段全钉）：
- Location: file:data/prometheus.lock（全系统唯一全局文件，存在即活动——transient 语义）。
- 字段：pid / dirty / cycle / ts / bottomReached（崩溃恢复语义的唯一承载）。
- 写侧纪律：原子写（.tmp + os.replace）；JSON indent=2（实测惯例）。
- 读侧纪律：stale 判定 = os.kill(pid, 0) 抛 ProcessLookupError → pid 不存活 = stale；
  只判定不删除（锁仅建议性独占提示，处置归 daemon 行为规格）。

锁文件以外零磁盘 IO；缺文件 = 无锁（None，transient 正常态），
文件存在但违反 5 字段契约 → LockFormatError fail loud。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "LockFormatError",
    "ProcessLockData",
    "lock_path",
    "read_lock",
    "write_lock",
    "is_stale",
]

LOCK_FILE_NAME = "prometheus.lock"


class LockFormatError(ValueError):
    """锁文件违反 ProcessLock 契约（缺字段/多字段/类型错/JSON 损坏）——fail loud。"""


@dataclass(frozen=True)
class ProcessLockData:
    """ProcessLock.yaml Definition.Table 5 字段全钉，键序 = 契约表序（JSON 逐字）。"""

    pid: int
    dirty: bool
    cycle: int
    ts: int
    bottomReached: bool

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "dirty": self.dirty,
            "cycle": self.cycle,
            "ts": self.ts,
            "bottomReached": self.bottomReached,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> ProcessLockData:
        expected = ["pid", "dirty", "cycle", "ts", "bottomReached"]
        if not isinstance(raw, dict):
            raise LockFormatError(f"lock payload must be a JSON object, got {type(raw).__name__}")
        missing = [k for k in expected if k not in raw]
        if missing:
            raise LockFormatError(f"lock file missing pinned fields: {missing}")
        unknown = [k for k in raw if k not in expected]
        if unknown:
            raise LockFormatError(f"lock file has unpinned extra fields: {unknown}")
        for k in ("pid", "cycle", "ts"):
            if type(raw[k]) is not int:
                raise LockFormatError(f"lock field {k!r} must be int, got {type(raw[k]).__name__}")
        for k in ("dirty", "bottomReached"):
            if type(raw[k]) is not bool:
                raise LockFormatError(f"lock field {k!r} must be bool, got {type(raw[k]).__name__}")
        return cls(
            pid=raw["pid"],
            dirty=raw["dirty"],
            cycle=raw["cycle"],
            ts=raw["ts"],
            bottomReached=raw["bottomReached"],
        )


def lock_path(data_root: Path | str) -> Path:
    """file:data/prometheus.lock——data_root 即契约的 data/ 根。"""
    return Path(data_root) / LOCK_FILE_NAME


def read_lock(path: Path | str) -> ProcessLockData | None:
    """读锁。文件不存在 → None（transient：不存在即无锁，非错误）。

    文件存在但损坏/违约 → LockFormatError（原子写保证撕裂读不可能，
    损坏必是外因，fail loud 优于静默当无锁）。
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LockFormatError(f"lock file {path} is corrupt: {exc}") from exc
    return ProcessLockData.from_dict(raw)


def write_lock(path: Path | str, lock: ProcessLockData) -> None:
    """原子写锁（.tmp + os.replace；JSON indent=2，utf-8，无尾随换行）。

    父目录缺失则先建（fresh clone 无 data/ 的首启场景；目录创建归本模块，
    与 writer 同款约定）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(lock.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_stale(lock: ProcessLockData) -> bool:
    """读侧 stale 判定：pid 不存活 = stale。只判定，不删文件。

    - ProcessLookupError → pid 不存活 → stale（契约：pid 不存活 = stale 锁）。
    - PermissionError → EPERM 恰证明进程存在（只是无权发信号）→ 非 stale。
      （存活判定取 POSIX 正解。）
    - 其他 OSError → 无法判定 → 非 stale（建议性锁宁可不动作）。
    """
    try:
        os.kill(lock.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False
    return False
