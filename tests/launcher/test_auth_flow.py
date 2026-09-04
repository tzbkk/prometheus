"""MD-119/120：start deepbackfill 浏览器扫码流 + Ctrl+C 安全面。"""

from __future__ import annotations

import urllib.error

from src.launcher.auth_flow import wait_for_web_login
from src.launcher.shell import Shell


class _FakeUrlopen:
    """脚本化 /auth/status 序列（缺省连接拒——服务未起常态）。"""

    def __init__(self, bodies=None):
        self.bodies = list(bodies or [])
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if not self.bodies:
            raise urllib.error.URLError("connection refused")
        import io
        import json as _json

        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body

        class _Resp(io.BytesIO):
            headers = {}
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp(_json.dumps(body).encode("utf-8"))


class _FakePm:
    def __init__(self, state):
        self._state = state

    def status_of(self, name):
        return {"state": self._state}


class _Recorder:
    def __init__(self):
        self.lines = []
        self.urls = []

    def echo(self, text):
        self.lines.append(str(text))

    def open(self, url):
        self.urls.append(url)
        return True


def test_start_deepbackfill_opens_browser_and_waits_for_login():
    rec = _Recorder()
    fake = _FakeUrlopen(
        [
            {"state": "qr_pending", "detail": "scan the QR", "qr_epoch": 1},
            {"state": "scanned", "detail": "confirm", "qr_epoch": 1},
            {"state": "ok", "detail": "logged in", "uin": "10001", "qr_epoch": 1},
        ]
    )
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(sec):
        clock["t"] += sec

    result = wait_for_web_login(
        9424, echo=rec.echo, open_browser=rec.open, urlopen=fake,
        sleep=sleep, now=now, boot_window=8.0, timeout=180.0, poll=0.5,
    )

    # ok 面：成功返回 status dict；浏览器开在扫码页；成功提示含 uin。
    assert result == {"state": "ok", "detail": "logged in", "uin": "10001", "qr_epoch": 1}
    assert rec.urls == ["http://127.0.0.1:9424/auth/page"]
    assert any("手机 QQ 扫码" in line for line in rec.lines)
    assert any("登录成功（uin=10001）" in line for line in rec.lines)

    # ok 快路径：boot 窗内首询即 ok → 不开浏览器、立即返回。
    rec2 = _Recorder()
    fake2 = _FakeUrlopen([{"state": "ok", "detail": "valid", "uin": "10001"}])
    result2 = wait_for_web_login(
        9424, echo=rec2.echo, open_browser=rec2.open, urlopen=fake2,
        sleep=lambda s: None, now=lambda: 0.0,
    )
    assert result2["state"] == "ok"
    assert rec2.urls == []
    assert any("凭证就绪" in line for line in rec2.lines)

    # 开浏览器失败降级：纯 URL 打印兜底。
    rec3 = _Recorder()
    fake3 = _FakeUrlopen(
        [{"state": "qr_pending", "detail": "d", "qr_epoch": 1}] * 4
        + [{"state": "ok", "detail": "ok", "uin": "10001"}]
    )
    clock3 = {"t": 0.0}
    result3 = wait_for_web_login(
        9424, echo=rec3.echo, open_browser=lambda url: False, urlopen=fake3,
        sleep=lambda s: clock3.__setitem__("t", clock3["t"] + s) if s else None,
        now=lambda: clock3["t"],
    )
    assert result3["state"] == "ok"
    assert any("请手动访问" in line and "/auth/page" in line for line in rec3.lines)

    # 状态不可达：boot 窗耗尽 → 可读提示 + None（不炸）。
    rec4 = _Recorder()
    result4 = wait_for_web_login(
        9424, echo=rec4.echo, open_browser=rec4.open, urlopen=_FakeUrlopen(),
        sleep=sleep, now=now, boot_window=1.0,
    )
    assert result4 is None
    assert any("不可达" in line for line in rec4.lines)
    assert rec4.urls == []

    # Shell 装配面：start deepbackfill 成功后钩子走等待流；auth 动词同流。
    shell = Shell(
        _FakePm("running"), {"deepbackfill_port": 9424}, "/tmp/x", dispatcher=None
    )
    shell._run_web_login_wait = lambda: rec.lines.append("WAIT-RAN")
    shell._wait_deepbackfill_auth()
    assert rec.lines[-1] == "WAIT-RAN"


def test_auth_wait_interrupt_safe_and_offline_hint(capsys):
    # Ctrl+C（等待中）：KeyboardInterrupt → 取消消息（会话保留/续扫指引）+ None。
    rec = _Recorder()
    clock = {"t": 0.0}

    def sleep(sec):
        clock["t"] += sec
        if clock["t"] >= 1.0:
            raise KeyboardInterrupt

    fake = _FakeUrlopen([{"state": "qr_pending", "detail": "d", "qr_epoch": 1}] * 20)
    result = wait_for_web_login(
        9424, echo=rec.echo, open_browser=rec.open, urlopen=fake,
        sleep=sleep, now=lambda: clock["t"],
    )
    assert result is None
    assert rec.urls == ["http://127.0.0.1:9424/auth/page"]  # 浏览器已开（续看入口）
    assert any("已取消等待" in line and "/auth/page" in line for line in rec.lines)

    # auth 动词·服务未跑：可读提示先 start deepbackfill（回提示符，零等待流）。
    shell = Shell(_FakePm("stopped"), {}, "/tmp/x", dispatcher=None)
    shell._handle_auth()
    out = capsys.readouterr().out
    assert "start deepbackfill" in out
    assert "未运行" in out
