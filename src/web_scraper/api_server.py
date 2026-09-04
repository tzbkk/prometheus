"""scraper HTTP API：:9420 五指令边，契约 components/scraper.yaml。

响应形：裸状态码 + 契约结构体；错误 = 裸状态码 + ErrorEnvelope
``{"error": {"code", "message"}}``。

    GET  /health                 200 ScraperHealth  {"scanned_feeds": int}
    GET  /stats                  200 ScraperStats   {"feeds","comments","replies","media"}
    GET  /config                 200 ScraperConfig  （guilds 薄钉 + 附加键宽容）
    PUT  /config                 200 ScraperConfig  回显形态：部分字段 →
                                  合并 → 响应完整新态全文；非对象体/含 guilds
                                  → 400 ErrorEnvelope（guilds 仅 conf 文件面）
    GET  /logs                   200 ScraperLogs    {"lines": [str]}（?tail=N
                                  查询参数归行为层——取尾 N 行）
    POST /action/trigger-daemon  200 ScraperAction  {"triggered": true}
                                  （幂等：守护已在跑亦 true——受理语义）

未知路由/方法 → 404 ErrorEnvelope。put 合并面为运行时覆写（echo-only），
行为参数（如间隔）下次启动生效——契约只钉回显形，不钉生效时机。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_DEFAULT_PORT = 9420
_LOG_TAIL_MAX = 500


class ScraperService:
    """ThreadingHTTPServer 同进程形态（DeepbackfillService/LauncherApi 同款）。"""

    def __init__(
        self,
        *,
        stats: dict,
        config_state: dict,
        trigger_callback=None,
        port: int = _DEFAULT_PORT,
    ):
        """Args:
            stats: 与 daemon 共享的快照 dict（scanned_feeds/feeds/comments/
                replies/media/log_buffer）。
            config_state: 配置新态全文（GET 回体 & PUT 合并基；guilds 必在）。
            trigger_callback: () -> None（受理后异步触发，幂等语义归 daemon）。
            port: 绑定端口（0 = OS 分配，测试惯例）。
        """
        self._requested_port = port
        self.port = None
        self.server = None
        self._thread = None
        self._stats = stats
        self._config_state = config_state
        self._config_lock = threading.Lock()
        self._trigger_callback = trigger_callback
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

            def _error(self, status_code, code, message):
                self._send_json(
                    status_code, {"error": {"code": code, "message": message}}
                )

            def _not_found(self):
                self._error(404, "not_found", "unknown route")

            def _read_body_json(self) -> object | None:
                """读请求体；空体 → {}；坏 JSON → None（调用方 400）。"""
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
                    self._send_json(200, {"scanned_feeds": outer._stats.get("scanned_feeds", 0)})
                elif parsed.path == "/stats":
                    self._send_json(200, outer._stats_view())
                elif parsed.path == "/config":
                    with outer._config_lock:
                        self._send_json(200, json.loads(json.dumps(outer._config_state)))
                elif parsed.path == "/logs":
                    self._send_json(200, outer._logs_view(parsed))
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
                callback = outer._trigger_callback
                if callback is not None:
                    threading.Thread(target=callback, daemon=True).start()
                self._send_json(200, {"triggered": True})

            def do_DELETE(self):
                self._not_found()

            def do_PATCH(self):
                self._not_found()

        self._handler_cls = Handler

    def _stats_view(self) -> dict:
        # Verbatim passthrough, no coercion——契约执法归编译 Schema（harness
        # 红 = 错造类型不被遮蔽，QA3 负例的根据）。
        return {
            "feeds": self._stats.get("feeds", 0),
            "comments": self._stats.get("comments", 0),
            "replies": self._stats.get("replies", 0),
            "media": self._stats.get("media", 0),
            "gateway_rejects": self._stats.get("gateway_rejects", 0),
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
