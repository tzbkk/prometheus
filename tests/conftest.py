"""Root conftest for the mandate suite (⑪).

两个房屋惯例：

1. 信号守卫（autouse）——os.kill / os.killpg 阻断。
2. tmp_path 数据根——一切落盘测试的数据根在 pytest tmp_path 下，
   port=0 分配临时端口，mock 只打最外层缝（urlopen / Popen / 时钟）。
"""

import os

import pytest


@pytest.fixture(autouse=True)
def no_real_signals(monkeypatch):
    """Block os.kill / os.killpg during tests.

    A MagicMock pid once leaked into os.getpgid() and killpg(1) murdered
    every user process on the machine. Tests that genuinely need real
    signals must restore them explicitly inside the test:
        monkeypatch.setattr(os, "kill", os_kill_original)
    """

    def _blocked(name, real_func):
        def guard(pid, sig, *args, **kwargs):
            if sig == 0:
                return real_func(pid, 0)
            raise RuntimeError(
                f"os.{name}(pid={pid}, sig={sig}) blocked by conftest guard: "
                "tests must patch signal syscalls, never send real ones "
                "(sig=0 liveness probes pass through)"
            )
        return guard

    monkeypatch.setattr(os, "kill", _blocked("kill", os.kill), raising=True)
    monkeypatch.setattr(os, "killpg", _blocked("killpg", os.killpg), raising=True)
    yield


@pytest.fixture
def data_root(tmp_path):
    """tmp_path 数据根惯例（§10.3）：返回全新 <tmp>/data 目录。

    需要落盘的测试把所有文件操作锚定在此（或 tmp_path 兄弟目录）下，
    绝不写入仓库树或真实 data/。
    """
    root = tmp_path / "data"
    root.mkdir()
    return root
