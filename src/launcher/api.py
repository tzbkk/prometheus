"""LauncherApi: launcher 契约 API 面：十路径，:9421。

契约 components/launcher.yaml（实现规格逐字）；响应形：
裸状态码 + 契约结构体；错误 = 裸状态码 + ErrorEnvelope
``{"error": {"code", "message"}}``。

    GET  /targets                      200 TargetList（三目标富对象快照）
    GET  /targets/{target}             200 TargetStatus（未知目标 → 404）
    POST /targets/{target}/start       200 TargetStatus（幂等：已运行
                                       → 200 当前态）
    POST /targets/{target}/stop        200 TargetStatus（幂等：已停 → 200
                                       stopped 态）
    POST /targets/{target}/restart     200 TargetStatus（stop+start 新态）
    GET  /targets/{target}/logs        200 LogTail {"lines": [str]}
                                       （?tail=N 查询参数归行为层）
    GET  /config                       200 LauncherConfig（guilds 薄钉 +
                                       附加键宽容）
    PUT  /config                       200 LauncherConfig 回显形态：部分
                                       字段 → 合并 → 响应完整新态全文 +
                                       原子落盘（launcher 单写者）；
                                       非对象体/含 guilds → 400 ErrorEnvelope
                                       （guilds 仅 conf 文件面——scraper 先例）
    POST /shutdown                     200 ShutdownAck {"accepted": true}，
                                       受理后依序优雅关停全部目标并退出

未知路由/方法 → 404 ErrorEnvelope。{target} 之家 = TargetName 三枚举
（scraper|deepbackfill|viewer）——组件图外的名字天然被拒。
"""

import json
import os
import tempfile
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .process_manager import TARGETS

_DEFAULT_PORT = 9421
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_MAX = 500


class LauncherApi:
    """ThreadingHTTPServer 同进程形态（ScraperService/DeepbackfillService 同款）。"""

    def __init__(
        self,
        process_manager,
        config=None,
        config_path=None,
        guilds=None,
        port=_DEFAULT_PORT,
        shutdown_callback=None,
    ):
        """Args:
            process_manager: ProcessManager（三目标监督面）。
            config: launcher 配置 dict（GET/PUT /config 状态；None → 空态）。
            config_path: 原子落盘路径（None → PUT 仅运行时合并，不落盘）。
            guilds: 频道清单（conf/guilds.conf.json 面；GET 回体必带——
                LauncherConfig 薄钉字段，PUT 不可改）。
            port: 绑定端口（0 = OS 分配，测试惯例）。
            shutdown_callback: /shutdown 受理后的进程退出回调（__main__
                注入 SIGINT 自杀；测试不传即仅停 API）。
        """
        self.pm = process_manager
        self._config = config if config is not None else {}
        self._config_path = config_path
        self._guilds = guilds if guilds is not None else []
        self._config_lock = threading.Lock()
        self._shutdown_callback = shutdown_callback
        self._requested_port = port
        self.port = None
        self.server = None
        self._thread = None
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

            def _invalid_target(self, target):
                self._error(
                    404,
                    "invalid_target",
                    "unknown supervision target: {0}".format(target),
                )

            def _read_body_json(self):
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
                parts = [p for p in parsed.path.split("/") if p]
                if parts == ["targets"]:
                    self._send_json(200, outer.pm.status_all())
                elif len(parts) == 2 and parts[0] == "targets":
                    if parts[1] not in TARGETS:
                        self._invalid_target(parts[1])
                        return
                    self._send_json(200, outer.pm.status_of(parts[1]))
                elif (
                    len(parts) == 3
                    and parts[0] == "targets"
                    and parts[2] == "logs"
                ):
                    if parts[1] not in TARGETS:
                        self._invalid_target(parts[1])
                        return
                    tail = outer._qs_int(parse_qs(parsed.query), "tail")
                    self._send_json(
                        200,
                        {"lines": outer._read_log_tail(parts[1], tail)},
                    )
                elif parts == ["config"]:
                    with outer._config_lock:
                        self._send_json(200, outer._config_view())
                else:
                    self._not_found()

            def do_POST(self):
                parts = [p for p in urlparse(self.path).path.split("/") if p]
                if parts == ["shutdown"]:
                    self._handle_shutdown()
                    return
                if len(parts) == 3 and parts[0] == "targets":
                    target, action = parts[1], parts[2]
                    if target not in TARGETS:
                        self._invalid_target(target)
                        return
                    if action == "start":
                        # 幂等：已运行 → 200 当前态（start 内建保留）。
                        outer.pm.start(target)
                    elif action == "stop":
                        outer.pm.stop(target)
                    elif action == "restart":
                        outer.pm.restart(target)
                    else:
                        self._not_found()
                        return
                    self._send_json(200, outer.pm.status_of(target))
                    return
                self._not_found()

            def do_PUT(self):
                parts = [p for p in urlparse(self.path).path.split("/") if p]
                if parts != ["config"]:
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
                    outer._config.update(body)
                    outer._persist_config()
                    self._send_json(200, outer._config_view())

            def do_DELETE(self):
                self._not_found()

            def do_PATCH(self):
                self._not_found()

            def _handle_shutdown(self):
                outer.pm.graceful_shutdown()
                self._send_json(200, {"accepted": True})

                def _finish():
                    outer.stop()
                    callback = outer._shutdown_callback
                    if callback is not None:
                        callback()

                threading.Thread(target=_finish, daemon=True).start()

        self._handler_cls = Handler

    def _config_view(self):
        """GET/PUT 回体：launcher 键全文 + guilds 薄钉（LauncherConfig 形）。"""
        view = dict(self._config)
        view["guilds"] = [dict(g) for g in self._guilds]
        return view

    def _persist_config(self):
        """原子落盘 launcher 键（guilds 不入本文件——conf/guilds.conf.json 面）。"""
        if not self._config_path:
            return
        payload = json.dumps(self._config, indent=2, ensure_ascii=False)
        dir_path = os.path.dirname(self._config_path) or "."
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, self._config_path)
        except OSError:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    def _read_log_tail(self, name, tail):
        path = self.pm.log_path(name)
        if not os.path.exists(path):
            return []
        if tail is None:
            tail = _LOG_TAIL_DEFAULT
        tail = max(0, min(tail, _LOG_TAIL_MAX))
        if tail == 0:
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            kept = deque(fh, maxlen=tail)
        return [line.rstrip("\n") for line in kept]

    @staticmethod
    def _qs_int(qs, key):
        vals = qs.get(key)
        if not vals:
            return None
        try:
            return int(vals[0])
        except (ValueError, TypeError):
            return None

    def start(self):
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", self._requested_port), self._handler_cls
        )
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

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
