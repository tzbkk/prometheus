"""Prometheus — QQ guild channel post archiver."""

import subprocess
from pathlib import Path


def _read_git_version() -> str:
    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, timeout=2,
            cwd=Path(__file__).resolve().parents[2],
        )
        if r.returncode == 0:
            return r.stdout.strip().lstrip("v")
    except Exception:
        pass
    return "unknown"


__version__ = _read_git_version()
