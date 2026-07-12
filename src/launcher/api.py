"""LauncherApi: HTTP control plane for the Prometheus launcher.

Exposes POST /start /stop /restart /shutdown and GET /status on 127.0.0.1.
All responses use the unified envelope: {"ok": bool, "data": {}, "error": ""}.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_DEFAULT_PORT = 9421
_HEALTH_TIMEOUT_ERROR = "health check timeout 30s"


class LauncherApi:
    def __init__(self, process_manager, port=_DEFAULT_PORT):
        self.pm = process_manager
        self._requested_port = port
        self.port = None
        self.server = None
        self._thread = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass

            def _send(self, status_code, envelope):
                payload = json.dumps(envelope).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _ok(self, data):
                self._send(200, {"ok": True, "data": data, "error": ""})

            def _fail(self, error, status_code=200):
                self._send(status_code, {"ok": False, "data": {}, "error": error})

            def _read_body(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    return parsed if isinstance(parsed, dict) else {}
                except (ValueError, UnicodeDecodeError):
                    return {}

            def do_GET(self):
                if self.path == "/status":
                    status = outer.pm.get_status()
                    self._ok(status)
                else:
                    self._fail("not found", status_code=404)

            def do_POST(self):
                path = self.path
                if path == "/start":
                    self._handle_start()
                elif path == "/stop":
                    self._handle_stop()
                elif path == "/restart":
                    self._handle_restart()
                elif path == "/shutdown":
                    self._handle_shutdown()
                else:
                    self._fail("not found", status_code=404)

            def do_DELETE(self):
                self._fail("not found", status_code=404)

            def do_PUT(self):
                self._fail("not found", status_code=404)

            def do_PATCH(self):
                self._fail("not found", status_code=404)

            def _handle_start(self):
                self._read_body()
                status = outer.pm.get_status()
                if status.get("qq") == "running":
                    self._ok({"qq": "already running"})
                    return
                outer.pm.start_qq()
                self._ok({"qq": "started"})

            def _handle_stop(self):
                self._read_body()
                outer.pm.stop_qq()
                self._ok({"qq": "stopped"})

            def _handle_restart(self):
                self._read_body()
                success, elapsed_ms = outer.pm.restart_qq()
                if success:
                    self._ok({"qq": "restarted", "health_check_ms": elapsed_ms})
                else:
                    self._fail(_HEALTH_TIMEOUT_ERROR)

            def _handle_shutdown(self):
                self._read_body()
                outer.pm.graceful_shutdown()
                self._send(200, {"ok": True, "data": {}, "error": ""})
                threading.Thread(target=outer.stop, daemon=True).start()

        self._handler_cls = Handler

    def start(self):
        self.server = HTTPServer(("127.0.0.1", self._requested_port), self._handler_cls)
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
            self.server = HTTPServer(("127.0.0.1", self._requested_port), self._handler_cls)
            self.port = self.server.server_address[1]
        self.server.serve_forever()
