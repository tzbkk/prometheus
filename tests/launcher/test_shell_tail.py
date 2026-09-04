"""logs 尾随期间的 SIGINT 处置（^C 回提示符，不退 shell）。

机理：__main__ 把 SIGINT 换成自定义处理器直接 sys.exit(0) →
Ctrl+C 产生 SystemExit 穿透 _tail_log 的 except KeyboardInterrupt → launcher
带全部目标陪葬。修法 = 尾随期间临时换入 default_int_handler，finally 无条件恢复。
"""

import os
import signal
import threading
import time
from types import SimpleNamespace

import pytest

from src.launcher.shell import Shell


_os_kill_original = os.kill  # 导入期取真身——守卫 fixture 活着时 os.kill 已是 guard


@pytest.fixture(autouse=True)
def _real_sigint(no_real_signals, monkeypatch):
    """conftest 信号守卫的显式豁免（其 docstring 条款）：本套件的被测对象
    就是真实 SIGINT——换入 default_int_handler 后 ^C 必须化为 KeyboardInterrupt。
    请求 no_real_signals 保证其守卫先就位，再还原真 kill（豁免只覆本模块）。"""
    monkeypatch.setattr(os, "kill", _os_kill_original)


def test_log_tail_sigint_returns_to_prompt_and_restores_handler(tmp_path, capsys):
    """真实 SIGINT 打在尾随循环上：进程级杀手处理器未被触达、tail 打
    [tail stopped] 正常返回、原处理器逐字恢复（同一性）——launcher 不死。"""
    fired = []

    def _launcher_wide_handler(signum, frame):  # 形制同 __main__._on_signal
        fired.append(signum)
        raise SystemExit(0)

    previous = signal.signal(signal.SIGINT, _launcher_wide_handler)
    try:
        log = tmp_path / "deepbackfill.log"
        log.write_text("line-1\n", encoding="utf-8")
        shell = Shell(
            pm=SimpleNamespace(log_path=lambda t: str(log)),
            config={},
            config_path=None,
            dispatcher=None,
        )

        def _append_then_interrupt():
            # 0.25s 落新行（首轮 sleep 期间）；主循环 1.0s 醒来读到并打印；
            # 1.4s 发 SIGINT（落在第二轮 sleep 内）——两段行为都被验到。
            time.sleep(0.25)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("tail-line\n")
            time.sleep(1.15)
            os.kill(os.getpid(), signal.SIGINT)

        sniper = threading.Thread(target=_append_then_interrupt)
        sniper.start()
        try:
            shell._tail_log("deepbackfill")  # 必须正常返回（SystemExit 不得穿透）
        finally:
            sniper.join()

        assert fired == []  # 进程级处理器从未被触达（换了处置）
        out = capsys.readouterr().out
        assert "tail-line" in out  # 尾随面活着（新行照打）
        assert "[tail stopped]" in out  # Ctrl+C 语义 = 停尾随回提示符
        assert signal.getsignal(signal.SIGINT) is _launcher_wide_handler  # 恢复
    finally:
        signal.signal(signal.SIGINT, previous)
