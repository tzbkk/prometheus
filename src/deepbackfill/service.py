"""deepbackfill HTTP API（:9424 五端点真实现）。

响应形：裸状态码 + 契约结构体；错误 = 裸状态码 + ErrorEnvelope
``{"error": {"code", "message"}}``。

    GET  /health                 200 DeepbackfillHealth  {"scanned_feeds": int}
                                   （真计数——runner 扫一面）
    GET  /stats                  200 DeepbackfillStats   {pages, feeds,
                                   comments, replies, media, running}
                                    （进度语义：deepbackfill 是一次性全史回填，
                                    较 ScraperStats 多 pages/running 两进度键——
                                    进度键是获取器
                                    差异（isFinish 全史 vs 5.5 月窗）的自然推论）
    GET  /config                 200 DeepbackfillConfig  （launcher 形态回显）
    PUT  /config                 200 DeepbackfillConfig  回显形态：部分字段
                                    → 合并 → 完整新态全文；非对象体/含 guilds
                                    → 400 ErrorEnvelope（guilds 仅 conf 文件面）
    GET  /logs                   200 DeepbackfillLogs    {"lines": [str]}
                                    （?tail=N 尾窗，查询参数归行为层）
    POST /action/trigger-daemon  200 DeepbackfillAction  {"triggered": true}
                                   —— 起 background 回填线程；运行中再触发 =
                                   409 busy ErrorEnvelope（与 scraper 的幂等
                                   受理语义不同：全史回填不可重入，重入即冲突）

纯网登录三端点（行为层——契约五边语义不动；二维码图片在
浏览器里展示）：

    GET  /auth/qr.png            200 image/png（ptqrshow 原图字节直出——
                                   服务端缓存当前会话码；无活跃会话 → 404
                                   ErrorEnvelope）
    GET  /auth/status            200 {"state": "ok"|"qr_pending"|"scanned"|
                                   "failed", "detail": str, "uin"?: str,
                                   "qr_epoch": int}——首询懒启动凭证探测
                                   （launcher start 流/浏览器页轮询即 boot 面）
    GET  /auth/page              200 text/html（内嵌字符串常量，零构建：
                                   <img src="/auth/qr.png"> + fetch 轮询
                                   /auth/status + qr_epoch 变更自动换图
                                   （cache-bust）+ 三态文案 + 成功绿字）

未知路由/方法 → 404 ErrorEnvelope。put 合并面为运行时覆写（echo-only）。

AuthSessionManager（本模块）：凭证探测状态机——probe 活测（AuthClient
拉一页 get_feeds）缺/失效 → 起 QR 会话后台线程（ptqrlogin 轮询 ≥1.5s/次
硬下限；65 过期自动换新码；success → complete → conf 0600 四键 → 活测 →
state=ok）；stop_event 语义学 scraper 媒体池 drain（SIGTERM 干净停不悬）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from src.deepbackfill.credentials import (
    Credentials,
    CredentialStore,
    mask_secret,
)
from src.deepbackfill.weblogin import QRState, WebLoginClient

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 9424
_LOG_TAIL_MAX = 500
QR_POLL_INTERVAL_SEC = 1.6          # ptqrlogin 轮询节奏（硬下限 1.5s——§3 协议护栏）
_QR_POLL_FLOOR_SEC = 1.5
_RESTART_COOLDOWN_SEC = 10.0        # failed 后 /auth/status 再询的重启冷却
_QR_PNG_GRACE_SEC = 2.5             # qr.png 首图宽限（会话线程取码竞态窗）
_THREAD_JOIN_TIMEOUT_SEC = 5.0

AUTH_PAGE_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>deepbackfill 扫码登录</title>
<style>
body{font-family:system-ui,sans-serif;max-width:480px;margin:48px auto;text-align:center;background:#fafafa}
img{width:280px;height:280px;border:1px solid #ddd;background:#fff}
#state{font-size:1.15em;margin:1em 0}
.ok{color:#0a7d00;font-weight:bold}
.hint{color:#666}
</style>
</head>
<body>
<h2>QQ 扫码登录（deepbackfill）</h2>
<img id="qr" src="/auth/qr.png" alt="二维码生成中…">
<p id="state">等待扫码…</p>
<p class="hint">请用手机 QQ 扫描上方二维码</p>
<script>
var epoch = -1;
function tick(){
  fetch("/auth/status").then(function(r){return r.json()}).then(function(s){
    if (s.qr_epoch !== undefined && s.qr_epoch !== epoch){
      epoch = s.qr_epoch;
      document.getElementById("qr").src = "/auth/qr.png?e=" + epoch;
    }
    var el = document.getElementById("state");
    if (s.state === "ok"){
      el.textContent = "登录成功（uin=" + (s.uin || "?") + "）——本页可关闭";
      el.className = "ok";
      return;
    }
    if (s.state === "scanned"){
      el.textContent = "已扫码，请在手机上确认";
    } else if (s.state === "failed"){
      el.textContent = "登录失败：" + (s.detail || "未知原因") + "（将自动重试）";
    } else {
      el.textContent = "等待扫码…";
    }
    setTimeout(tick, 1500);
  }).catch(function(){ setTimeout(tick, 3000); });
}
tick();
</script>
</body>
</html>
"""


class AuthSessionManager:
    """凭证探测 + QR 会话状态机（/auth/* 三端点的后端）。

    生命周期：懒启动（首个 /auth/status 查询或 trigger 活测触发 probe）；
    probe = AuthClient 拉一页 get_feeds——成功 state=ok；CredentialsError/
    AuthError → 起 QR 会话线程。QR 线程：fetch_qr → 循环 poll_once（间隔
    ≥1.5s 硬下限）→ 67 已扫待确认 / 65 自动换新码（qr_epoch 前进——页面
    据此 cache-bust 换图）/ 0 成功 → complete → conf 0600 四键 → 活测 →
    ok。线程内任何失败 → state=failed（可读 detail），冷却后由下一次
    /auth/status 查询重启（浏览器页轮询即自愈面）。stop() = SIGTERM 停机
    缝（stop_event + join——学媒体池 drain，不留孤儿线程）。
    """

    def __init__(
        self,
        *,
        store: CredentialStore,
        probe,
        weblogin_factory=None,
        poll_interval: float = QR_POLL_INTERVAL_SEC,
        sleep=time.sleep,
        now_ms=None,
        restart_cooldown: float = _RESTART_COOLDOWN_SEC,
        on_ready=None,
    ):
        """Args:
            store: conf 四键读写居所（complete 产物落盘）。
            probe: () -> str——活测（拉一页 get_feeds），返回 uin；缺/失效
                抛 CredentialsError/AuthError（__main__ 组装）。
            weblogin_factory: () -> WebLoginClient（测试注伪缝；缺省真客户端）。
            poll_interval: ptqrlogin 轮询间隔（硬下限 1.5s——低于即抬升）。
            sleep: 间隔缝（测试注入记录）。
            now_ms: () -> int epoch ms（minted_at 面；缺省墙钟）。
            restart_cooldown: failed → 允许 /auth/status 重启探测的冷却秒。
            on_ready: () -> None——auth 到 ok（扫码或探测直通）后回调一次
                （__main__ 接线自动点火全史回填——start 之后
                全自动，trigger API 仅留手工面）。
        """
        self._store = store
        self._probe = probe
        self._weblogin_factory = weblogin_factory or WebLoginClient
        self._poll_interval = max(poll_interval, _QR_POLL_FLOOR_SEC)
        self._sleep = sleep
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._restart_cooldown = restart_cooldown
        self._on_ready = on_ready

        self._lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "qr_pending"
        self._detail = "starting credential probe / QR session"
        self._uin = ""
        self._qr_png: bytes | None = None
        self._qr_epoch = 0
        self._last_fail_ts = 0.0

    # ------------------------------------------------------------------
    # 观测面（/auth/* 端点消费）
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """状态快照；顺带懒启动/冷却重启探测线程（浏览器页轮询即自愈面）。"""
        self._kick()
        with self._lock:
            body = {
                "state": self._state,
                "detail": self._detail,
                "qr_epoch": self._qr_epoch,
            }
            if self._uin:
                body["uin"] = self._uin
            return body

    def qr_png(self) -> bytes | None:
        with self._lock:
            return self._qr_png

    def wait_qr_png(self, grace_sec: float) -> bytes | None:
        deadline = time.monotonic() + grace_sec
        while True:
            with self._lock:
                if self._qr_png is not None:
                    return self._qr_png
            if self._stop_event.is_set() or time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._state == "ok"

    def ensure_ready(self) -> bool:
        """同步活测一次（trigger 面消费）：ok 直真；缺/失效起 QR 会话。"""
        if not self.ready:
            self._run_probe()
        if self.ready:
            return True
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._start_thread()
        return False

    def stop(self) -> None:
        """SIGTERM 停机缝：stop_event 置位 + join（≤5s，不悬）。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_THREAD_JOIN_TIMEOUT_SEC)
        self._thread = None

    # ------------------------------------------------------------------
    # 内部面
    # ------------------------------------------------------------------
    def _kick(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        with self._lock:
            failed = self._state == "failed"
            ts = self._last_fail_ts
        if thread is not None and failed and (time.monotonic() - ts) < self._restart_cooldown:
            return
        if thread is None or (failed and not thread.is_alive()):
            self._start_thread()

    def _start_thread(self) -> None:
        if self._stop_event.is_set():
            return
        self._thread = threading.Thread(
            target=self._session_loop, name="deepbackfill-auth", daemon=True
        )
        self._thread.start()

    def _set(self, state: str, detail: str, *, uin: str | None = None,
             qr: bytes | None = None, keep_qr: bool = True) -> None:
        with self._lock:
            self._state = state
            self._detail = detail
            if uin is not None:
                self._uin = uin
            if qr is not None or not keep_qr:
                self._qr_png = qr

    def _run_probe(self) -> None:
        with self._probe_lock:
            if self._stop_event.is_set():
                return
            try:
                uin = self._probe()
            except Exception as exc:  # noqa: BLE001 —— 缺/失效统一面：起 QR 会话
                logger.warning("credential probe failed: %s", exc)
                return
            self._set("ok", f"credentials valid (uin={uin})", uin=uin, qr=None, keep_qr=False)

    def _session_loop(self) -> None:
        self._run_probe()
        while not self._stop_event.is_set() and not self.ready:
            try:
                self._qr_session()
            except Exception as exc:  # noqa: BLE001 —— 会话级兜底：failed + 冷却重启
                self._fail(f"QR session error: {exc}")
                return
            if self._stop_event.is_set() or self.ready:
                break
            if self._wait(_RESTART_COOLDOWN_SEC):
                return
        if self.ready and self._on_ready is not None:
            try:
                self._on_ready()
            except Exception as exc:  # noqa: BLE001 —— 点火失败可读面，不吞 ok 态
                logger.error("on_ready (auto backfill) failed: %s", exc)

    def _qr_session(self) -> None:
        client = self._weblogin_factory()
        while not self._stop_event.is_set():
            png, qrsig = client.fetch_qr()
            with self._lock:
                self._qr_png = png
                self._qr_epoch += 1
                self._state = "qr_pending"
                self._detail = "scan the QR code at /auth/qr.png (mobile QQ)"
            while not self._stop_event.is_set():
                result = client.poll_once(qrsig)
                if result.state is QRState.EXPIRED:
                    logger.info("QR expired (code 65) — fetching a fresh one")
                    break  # 内层 break → 外层 fetch_qr 换新码
                if result.state is QRState.SCANNED:
                    who = f" as {result.nickname!r}" if result.nickname else ""
                    self._set("scanned", f"scanned{who} — confirm on your phone")
                elif result.state is QRState.WAITING:
                    self._set("qr_pending", "scan the QR code at /auth/qr.png (mobile QQ)")
                else:  # SUCCESS
                    self._on_success(client, result.check_url)
                    return
                if self._wait(self._poll_interval):
                    return
            if self._wait(0.2):  # 换码间隔（非 ptqrlogin 轮询——服务端喘息）
                return

    def _on_success(self, client, check_url: str) -> None:
        creds_raw = client.complete(check_url)
        creds = Credentials(
            uin=creds_raw["uin"],
            p_uin=creds_raw["p_uin"],
            p_skey=creds_raw["p_skey"],
            minted_at=self._now_ms(),
        )
        self._store.save(creds)
        try:
            uin = self._probe()
        except Exception as exc:  # noqa: BLE001 —— 登录成功但活测失败：failed 呈现
            self._fail(
                f"login ok but live verify failed (p_skey {mask_secret(creds.p_skey)}): {exc}"
            )
            return
        self._set("ok", f"logged in (uin={uin})", uin=uin, qr=None, keep_qr=False)
        logger.info("web QR login complete — uin=%s", uin)

    def _fail(self, detail: str) -> None:
        with self._lock:
            self._state = "failed"
            self._detail = detail
            self._last_fail_ts = time.monotonic()

    def _wait(self, seconds: float) -> bool:
        """可中断等待（sleep 缝——测试零真实等待；返回 True = 停机请求）。"""
        if self._stop_event.is_set():
            return True
        self._sleep(seconds)
        return self._stop_event.is_set()



class DeepbackfillService:
    """ThreadingHTTPServer 同进程形态（ScraperService/LauncherApi 同款）。"""

    def __init__(
        self,
        *,
        stats: dict,
        config_state: dict,
        trigger=None,
        stats_view=None,
        port: int = _DEFAULT_PORT,
        auth_session: AuthSessionManager | None = None,
    ):
        """Args:
            stats: 与 runner 共享的快照 dict（scanned_feeds/log_buffer/…）。
            config_state: 配置新态全文（GET 回体 & PUT 合并基；guilds 必在）。
            trigger: () -> bool——True = 回填线程已起，False = 运行中（409 busy）。
            stats_view: () -> dict——/stats 进度视图（缺省直读 stats dict 的
                四键；生产传 runner.live_stats——媒体计数现算）。
            port: 绑定端口（0 = OS 分配，测试惯例）。
            auth_session: 纯网登录会话管理器（/auth/* 三端点后端 +
                trigger 前置活测；None = 未装配——/auth/status 报 failed）。
        """
        self._requested_port = port
        self.port = None
        self.server = None
        self._thread = None
        self._stats = stats
        self._config_state = config_state
        self._config_lock = threading.Lock()
        self._trigger = trigger
        self._stats_view = stats_view
        self._auth_session = auth_session
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass

            def _send_json(self, status_code, body):
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _send_bytes(self, status_code, content_type, payload):
                self.send_response(status_code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _error(self, status_code, code, message):
                self._send_json(
                    status_code, {"error": {"code": code, "message": message}}
                )

            def _not_found(self):
                self._error(404, "not_found", "unknown route")

            def _read_body_json(self) -> object | None:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return None

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send_json(
                        200, {"scanned_feeds": outer._stats.get("scanned_feeds", 0)}
                    )
                elif parsed.path == "/stats":
                    self._send_json(200, outer._stats_snapshot())
                elif parsed.path == "/config":
                    with outer._config_lock:
                        self._send_json(
                            200, json.loads(json.dumps(outer._config_state))
                        )
                elif parsed.path == "/logs":
                    self._send_json(200, outer._logs_view(parsed))
                elif parsed.path == "/auth/qr.png":
                    session = outer._auth_session
                    png = (
                        session.wait_qr_png(_QR_PNG_GRACE_SEC)
                        if session is not None else None
                    )
                    if png is None:
                        self._error(
                            404,
                            "no_qr",
                            "no active QR session — credentials may already be valid, "
                            "or a session is starting (watch GET /auth/status)",
                        )
                    else:
                        self._send_bytes(200, "image/png", png)
                elif parsed.path == "/auth/status":
                    session = outer._auth_session
                    body = (
                        session.status()
                        if session is not None
                        else {"state": "failed", "detail": "auth session not configured"}
                    )
                    self._send_json(200, body)
                elif parsed.path == "/auth/page":
                    self._send_bytes(
                        200, "text/html; charset=utf-8", AUTH_PAGE_HTML.encode("utf-8")
                    )
                else:
                    self._not_found()

            def do_PUT(self):
                if urlparse(self.path).path != "/config":
                    self._not_found()
                    return
                body = self._read_body_json()
                if not isinstance(body, dict):
                    self._error(400, "bad_request", "config body must be a JSON object")
                    return
                if "guilds" in body:
                    self._error(
                        400,
                        "guilds_readonly",
                        "guilds cannot be updated via API; edit conf/guilds.conf.json and restart",
                    )
                    return
                with outer._config_lock:
                    outer._config_state.update(body)
                    self._send_json(
                        200, json.loads(json.dumps(outer._config_state))
                    )

            def do_POST(self):
                if urlparse(self.path).path != "/action/trigger-daemon":
                    self._not_found()
                    return
                self._read_body_json()
                session = outer._auth_session
                if session is not None and not session.ensure_ready():
                    self._error(
                        409,
                        "not_configured",
                        "credentials not ready — a QR login session is now active: "
                        "scan the code at /auth/page ('start deepbackfill' opens the "
                        "browser automatically), then re-trigger",
                    )
                    return
                callback = outer._trigger
                if callback is None:
                    self._error(
                        409,
                        "not_configured",
                        "backfill not configured — no auth session on this service",
                    )
                    return
                if not callback():
                    self._error(
                        409,
                        "busy",
                        "backfill already running — wait for isFinish (watch GET /stats)",
                    )
                    return
                self._send_json(200, {"triggered": True})

            def do_DELETE(self):
                self._not_found()

            def do_PATCH(self):
                self._not_found()

        self._handler_cls = Handler

    def _stats_snapshot(self) -> dict:
        if self._stats_view is not None:
            return self._stats_view()
        return {
            "pages": self._stats.get("pages", 0),
            "feeds": self._stats.get("feeds", 0),
            "comments": self._stats.get("comments", 0),
            "replies": self._stats.get("replies", 0),
            "media": self._stats.get("media", 0),
            "running": bool(self._stats.get("running", False)),
        }

    def _logs_view(self, parsed) -> dict:
        qs = parse_qs(parsed.query)
        entries = [
            entry
            for entry in (self._stats.get("log_buffer") or [])
            if isinstance(entry, dict)
        ]
        tail = self._qs_int(qs, "tail", len(entries))
        tail = max(0, min(tail, _LOG_TAIL_MAX))
        lines = [f"[{e.get('level', 'INFO')}] {e.get('msg', '')}" for e in entries]
        return {"lines": lines[-tail:] if tail else []}

    @staticmethod
    def _qs_int(qs, key, default):
        vals = qs.get(key)
        if not vals:
            return default
        try:
            return int(vals[0])
        except (ValueError, TypeError):
            return default

    def start(self):
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", self._requested_port), self._handler_cls
        )
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        server = self.server
        if server is not None:
            self.server = None
            server.shutdown()
            server.server_close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

    def serve_forever(self):
        if self.server is None:
            self.server = ThreadingHTTPServer(
                ("127.0.0.1", self._requested_port), self._handler_cls
            )
            self.port = self.server.server_address[1]
        self.server.serve_forever()
