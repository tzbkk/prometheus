"""Dynamic version source for the prometheus project.

版本机制：``__version__`` 读 ``git describe --tags --abbrev=0``（剥离 ``v``
前缀）；仓库尚无 tag 时回退 1.0.0-rc.1。首个 tag（v1.0.0）由作者在
演进完成后亲自建立，届时 describe 自动接管。
"""

import subprocess
from pathlib import Path

_FALLBACK_VERSION = "1.0.0-rc.1"


def _read_git_version() -> str:
    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, timeout=2,
            cwd=Path(__file__).resolve().parents[1],
        )
        if r.returncode == 0:
            return r.stdout.strip().lstrip("v")
    except Exception:
        pass
    return _FALLBACK_VERSION


__version__ = _read_git_version()
