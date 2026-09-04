"""MD-106/107：service 五端点活体（/stats 进度 + trigger 409 busy）。
MD-117/118：/auth/* 三端点 + 凭证探测状态机。"""

from __future__ import annotations

import json
import stat
import threading
import time

from src.deepbackfill.credentials import CredentialsError, CredentialStore
from src.deepbackfill.service import AUTH_PAGE_HTML, AuthSessionManager, DeepbackfillService
from src.deepbackfill.weblogin import QRState, PollResult

from tests.deepbackfill.conftest import (
    CHECK_URL,
    GUILD,
    NICKNAME,
    P_SKEY,
    TINY_PNG,
    UIN,
    NoopSleep,
)


class _Gate:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def block(self):
        self.started.set()
        self.release.wait()


def _config_state():
    return {
        "apiVersion": "2",
        "guilds": [{"guild_id": GUILD, "guild_number": "Takagi3channel", "name": "g"}],
        "deepbackfill_api_port": 9424,
    }


_DEFAULT_TRIGGER = object()


def _live_service(trigger=_DEFAULT_TRIGGER, stats_view=None, stats=None, auth_session=None):
    stats = stats if stats is not None else {
        "scanned_feeds": 0, "pages": 0, "feeds": 0, "comments": 0,
        "replies": 0, "media": 0, "running": False, "log_buffer": [
            {"seq": 1, "level": "INFO", "msg": "page 1 isFinish=false", "ts": "t"},
            {"seq": 2, "level": "INFO", "msg": "page 2 isFinish=true", "ts": "t"},
        ],
    }
    service = DeepbackfillService(
        stats=stats,
        config_state=_config_state(),
        trigger=(lambda: True) if trigger is _DEFAULT_TRIGGER else trigger,
        stats_view=stats_view,
        port=0,
        auth_session=auth_session,
    )
    service.start()
    return service, stats


def test_service_stats_reports_backfill_progress(http, schema_assert):
    service, stats = _live_service()
    try:
        stats.update(
            {"pages": 604, "scanned_feeds": 6016, "running": True}
        )
        status, body = http("GET", f"http://127.0.0.1:{service.port}/stats")
        assert status == 200
        schema_assert(body, "DeepbackfillStats")
        assert body["pages"] == 604
        assert body["running"] is True

        status, health = http("GET", f"http://127.0.0.1:{service.port}/health")
        assert status == 200
        schema_assert(health, "DeepbackfillHealth")
        assert health["scanned_feeds"] == 6016  # 真计数

        status, logs = http("GET", f"http://127.0.0.1:{service.port}/logs?tail=1")
        assert status == 200
        schema_assert(logs, "DeepbackfillLogs")
        assert logs["lines"] == ["[INFO] page 2 isFinish=true"]

        status, cfg = http("GET", f"http://127.0.0.1:{service.port}/config")
        assert status == 200
        schema_assert(cfg, "DeepbackfillConfig")
        assert cfg["guilds"][0]["guild_id"] == GUILD

        status, merged = http(
            "PUT",
            f"http://127.0.0.1:{service.port}/config",
            {"deepbackfill_api_port": 9424},
        )
        assert status == 200
        assert merged == cfg  # ㉖ 回显形态：合并 → 完整新态全文

        for method, payload in (
            ("PUT", {"guilds": []}),
        ):
            status, err = http(method, f"http://127.0.0.1:{service.port}/config", payload)
            assert status == 400
            schema_assert(err, "ErrorEnvelope")
    finally:
        service.stop()


def test_service_trigger_busy_returns_409_envelope(http, schema_assert):
    gate = _Gate()
    state = {"thread": None}

    def mirror_runner_start():
        # 镜像 runner.start_background 语义：起线程返回 True，
        # 线程仍活时再调用返回 False（409 busy 判定源）。
        thread = state["thread"]
        if thread is not None and thread.is_alive():
            return False
        state["thread"] = threading.Thread(target=gate.block, daemon=True)
        state["thread"].start()
        return True

    service, stats = _live_service(trigger=mirror_runner_start)
    try:
        status, body = http(
            "POST", f"http://127.0.0.1:{service.port}/action/trigger-daemon"
        )
        assert status == 200
        schema_assert(body, "DeepbackfillAction")
        assert body["triggered"] is True

        assert gate.started.wait(5)
        status, busy = http(
            "POST", f"http://127.0.0.1:{service.port}/action/trigger-daemon"
        )
        assert status == 409
        schema_assert(busy, "ErrorEnvelope")
        assert busy["error"]["code"] == "busy"

        gate.release.set()
        status, again = http(
            "POST", f"http://127.0.0.1:{service.port}/action/trigger-daemon"
        )
        assert status == 200 and again["triggered"] is True
    finally:
        gate.release.set()
        service.stop()

    # trigger=None（未配置凭据面）→ 409 not_configured（指向 shell auth）。
    unconfigured, _ = _live_service(trigger=None)
    try:
        status, err = http(
            "POST", f"http://127.0.0.1:{unconfigured.port}/action/trigger-daemon"
        )
        assert status == 409
        schema_assert(err, "ErrorEnvelope")
        assert err["error"]["code"] == "not_configured"
        assert "auth" in err["error"]["message"]
    finally:
        unconfigured.stop()


# ---------------------------------------------------------------------------
# MD-117/118：伪 weblogin 会话 + 探测状态机 + 三端点。
# ---------------------------------------------------------------------------

class _ScriptedWebLogin:
    """AuthSessionManager 的 weblogin 缝替身：脚本化 fetch/poll/complete。

    poll_once 入口经 StepGate 放行——瞬态（qr_pending/scanned/65 换码）
    可被测试确定性观察（NoopSleep 零等待下不门控会瞬变）。脚本耗尽后
    poll 恒回 WAITING（端点测试需要稳定 qr_pending 态）；complete 后经
    on_complete 钩子翻转 probe（活测在凭证落盘后即通过——生产语义）。
    """

    def __init__(self, script: list, gate: "_StepGate | None" = None):
        self._script = list(script)  # 元素：PNG 字节 | PollResult | dict(complete 产物)
        self.gate = gate
        self.on_complete = None
        self.fetch_calls = 0
        self.poll_calls = 0

    def fetch_qr(self):
        self.fetch_calls += 1
        item = self._script.pop(0)
        assert isinstance(item, bytes), f"script expected PNG bytes, got {item!r}"
        return item, f"qrsig-{self.fetch_calls}"

    def poll_once(self, qrsig):
        if self.gate is not None:
            self.gate.wait()
        self.poll_calls += 1
        if not self._script:
            return PollResult(QRState.WAITING, code=66)
        item = self._script.pop(0)
        assert isinstance(item, PollResult), f"script expected PollResult, got {item!r}"
        return item

    def complete(self, check_url):
        item = self._script.pop(0)
        assert isinstance(item, dict), f"script expected complete dict, got {item!r}"
        if self.on_complete is not None:
            self.on_complete(item)
        return item


class _StepGate:
    """每 release 放行一次（信号量——双 release 不合并）。"""

    def __init__(self):
        self._sem = threading.Semaphore(0)

    def wait(self):
        assert self._sem.acquire(timeout=5), "step gate timeout"

    def release(self):
        self._sem.release()


class _ProbeBox:
    """probe 缝替身：按开关决定活测成败（缺/失效 → 起 QR 会话）。"""

    def __init__(self):
        self.valid = False
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.valid:
            raise CredentialsError("no credentials — QR required")
        return str(UIN)


def _wait_state(manager, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    body = manager.status()
    while time.monotonic() < deadline and body["state"] != want:
        time.sleep(0.02)
        body = manager.status()
    return body


def _wait_qr_epoch(manager, epoch=1, timeout=5.0):
    """等真二维码：构造器初态即 qr_pending/epoch 0，只等状态会命中占位快照。"""
    deadline = time.monotonic() + timeout
    body = manager.status()
    while time.monotonic() < deadline and body["qr_epoch"] < epoch:
        time.sleep(0.02)
        body = manager.status()
    return body


def test_service_auth_probe_state_machine(tmp_path):
    store = CredentialStore(tmp_path / "deepbackfill.conf.json")
    probe = _ProbeBox()
    sleep = NoopSleep()
    gate = _StepGate()
    script = [
        TINY_PNG,                                    # 第一枚 QR
        PollResult(QRState.WAITING, code=66),
        PollResult(QRState.SCANNED, nickname=NICKNAME, code=67),
        PollResult(QRState.EXPIRED, code=65),       # 65 → 换新码
        TINY_PNG + TINY_PNG,                         # 第二枚 QR（字节可区分）
        PollResult(QRState.SUCCESS, check_url=CHECK_URL, code=0),
        {"p_skey": P_SKEY, "p_uin": f"o{UIN}", "uin": str(UIN)},
    ]
    fake = _ScriptedWebLogin(script, gate=gate)
    fake.on_complete = lambda creds: setattr(probe, "valid", True)
    manager = AuthSessionManager(
        store=store,
        probe=probe,
        weblogin_factory=lambda: fake,
        sleep=sleep,
        now_ms=lambda: 1782919600999,
    )
    try:
        # 探测失败（缺凭证）→ QR 会话起：poll#1 门口——qr_pending/e1/png1 可观。
        body = _wait_qr_epoch(manager)
        assert body["state"] == "qr_pending" and manager.qr_png() == TINY_PNG
        gate.release()                # poll#1 → 66
        gate.release()                # poll#2 → 67（瞬态跳过观察）
        body = _wait_state(manager, "scanned")  # poll#3 门口
        assert NICKNAME in body["detail"]
        gate.release()                # poll#3 → 65 换码

        # 65 过期：自动换新码——epoch 前进、PNG 换新。
        body = _wait_state(manager, "qr_pending")
        assert body["qr_epoch"] == 2
        assert manager.qr_png() == TINY_PNG + TINY_PNG
        gate.release()                # poll#4 → 0 成功 → complete → 活测

        body = _wait_state(manager, "ok", timeout=8.0)
        assert body["uin"] == str(UIN)
        assert probe.calls >= 2  # boot 探测 + 登录后活测

        # conf 落盘：四键 + 0600。
        conf = tmp_path / "deepbackfill.conf.json"
        doc = json.loads(conf.read_text(encoding="utf-8"))
        assert set(doc) == {"uin", "p_uin", "p_skey", "minted_at"}
        assert doc["p_skey"] == P_SKEY and doc["minted_at"] == 1782919600999
        assert stat.S_IMODE(conf.stat().st_mode) == 0o600

        # 轮询节奏：全部 ptqrlogin 间隔 ≥1.5s（0.2 是换码喘息，非轮询）。
        poll_waits = [s for s in sleep.calls if s >= 1.0]
        assert poll_waits and all(s >= 1.5 for s in poll_waits), (
            f"ptqrlogin 轮询节奏必须 ≥1.5s/次: {sleep.calls}"
        )

        # ensure_ready：ok 直真（trigger 面）；SIGTERM 式停机 join 干净。
        assert manager.ensure_ready() is True
    finally:
        manager.stop()
    assert manager._thread is None or not manager._thread.is_alive()


def test_service_auth_endpoints_qr_png_status_page(http, schema_assert, tmp_path):
    store = CredentialStore(tmp_path / "deepbackfill.conf.json")
    probe = _ProbeBox()
    fake = _ScriptedWebLogin(
        [
            TINY_PNG,
            PollResult(QRState.WAITING, code=66),
            PollResult(QRState.WAITING, code=66),
        ]
    )
    manager = AuthSessionManager(
        store=store,
        probe=probe,
        weblogin_factory=lambda: fake,
        sleep=lambda s: None,
    )
    service, _ = _live_service(trigger=None, auth_session=manager)
    try:
        _wait_qr_epoch(manager)

        # /auth/qr.png：PNG 原图直出（真魔数）；无会话面由 404 分支覆盖（下）。
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{service.port}/auth/qr.png", timeout=5
        ) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "image/png"
            png = resp.read()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and png == TINY_PNG

        # /auth/status：状态机 JSON（state/detail/qr_epoch）。
        status, body = http("GET", f"http://127.0.0.1:{service.port}/auth/status")
        assert status == 200
        assert body["state"] == "qr_pending"
        assert body["qr_epoch"] == 1
        assert isinstance(body["detail"], str)

        # /auth/page：内嵌 HTML——img src + fetch 轮询 + cache-bust 换图 + 文案。
        with urllib.request.urlopen(
            f"http://127.0.0.1:{service.port}/auth/page", timeout=5
        ) as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers.get("Content-Type", "")
            html = resp.read().decode("utf-8")
        assert html == AUTH_PAGE_HTML
        for marker in (
            'src="/auth/qr.png"',
            'fetch("/auth/status")',
            '"/auth/qr.png?e=" + epoch',
            "等待扫码",
            "请在手机上确认",
            "登录成功",
        ):
            assert marker in html, marker

        # trigger 未就绪：409 not_configured（活测失败即起会话，消息指 QR 页）。
        status, err = http(
            "POST", f"http://127.0.0.1:{service.port}/action/trigger-daemon"
        )
        assert status == 409
        schema_assert(err, "ErrorEnvelope")
        assert err["error"]["code"] == "not_configured"
        assert "/auth/page" in err["error"]["message"]
    finally:
        service.stop()
        manager.stop()

    # 无活跃会话：qr.png → 404 ErrorEnvelope；未装配 auth_session 的 status 面。
    bare, _ = _live_service(trigger=None)
    try:
        status, err = http("GET", f"http://127.0.0.1:{bare.port}/auth/qr.png")
        assert status == 404
        schema_assert(err, "ErrorEnvelope")
        status, body = http("GET", f"http://127.0.0.1:{bare.port}/auth/status")
        assert body["state"] == "failed"
    finally:
        bare.stop()


def test_service_auth_ready_fires_on_ready_once(tmp_path):
    """MD-123 auth 到 ok → on_ready 恰一次（扫码成功路径 + 探测直通路径）
    ——__main__ 接线自动点火全史回填（start 之后全自动，
    trigger API 仅留手工面）。"""
    def _fired_once(fired):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not fired:
            time.sleep(0.05)
        return fired == [1]

    # 路径 A：扫码成功 → complete → 活测 ok → on_ready。
    fake = _ScriptedWebLogin(
        [TINY_PNG,
         PollResult(QRState.SUCCESS, check_url=CHECK_URL, code=0),
         {"p_skey": P_SKEY, "p_uin": f"o{UIN}", "uin": str(UIN)}],
        gate=_StepGate(),
    )
    probe = _ProbeBox()
    fake.on_complete = lambda creds: setattr(probe, "valid", True)
    fired_a: list[int] = []
    mgr_a = AuthSessionManager(
        store=CredentialStore(tmp_path / "a.conf.json"), probe=probe,
        weblogin_factory=lambda: fake, sleep=NoopSleep(),
        now_ms=lambda: 1, on_ready=lambda: fired_a.append(1),
    )
    try:
        body = _wait_qr_epoch(mgr_a)
        assert body["state"] == "qr_pending" and body["qr_epoch"] == 1
        fake.gate.release()  # poll#1 → SUCCESS
        assert _wait_state(mgr_a, "ok", timeout=8.0)["uin"] == str(UIN)
        assert _fired_once(fired_a)
        mgr_a.status()  # ok 后再询不重 fire（线程已收，无冷却重启）
        time.sleep(0.2)
        assert fired_a == [1]
    finally:
        mgr_a.stop()

    # 路径 B：开机探测直通（已有有效凭证）→ on_ready。
    probe_b = _ProbeBox()
    probe_b.valid = True
    fired_b: list[int] = []
    mgr_b = AuthSessionManager(
        store=CredentialStore(tmp_path / "b.conf.json"), probe=probe_b,
        sleep=NoopSleep(), now_ms=lambda: 1, on_ready=lambda: fired_b.append(1),
    )
    try:
        assert _wait_state(mgr_b, "ok", timeout=8.0)["state"] == "ok"
        assert _fired_once(fired_b)
    finally:
        mgr_b.stop()
