"""HTTP API server for the web scraper.

Exposes health, stats, config, logs, and action endpoints on 127.0.0.1.
Uses the project-standard envelope: {"ok": bool, "data": {}, "error": ""}.
Based on http.server (stdlib only, matching launcher/api.py pattern).

Endpoints (polled by src/tui/api_client.py):
    GET  /health                       liveness + scanned_feeds count
    GET  /stats                        feeds/comments/media/daemon stats
    GET  /config                       current scraper config
    PUT  /config                       update scraper config
    GET  /logs?since=N&max=M           sliced log_buffer entries
    POST /action/trigger-daemon        manually trigger a daemon scan
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

_DEFAULT_PORT = 9420


class APIServer:
    def __init__(self, store, stats, port=_DEFAULT_PORT):
        self.store = store
        self.stats = stats
        self._requested_port = port
        self.port = None
        self.server = None
        self._thread = None
        self._trigger_callback = None
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
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/health":
                    self._handle_health()
                elif path == "/stats":
                    self._handle_stats()
                elif path == "/config":
                    self._handle_config_get()
                elif path == "/logs":
                    self._handle_logs(parsed)
                else:
                    self._fail("not found", status_code=404)

            def do_POST(self):
                path = self.path
                if path == "/action/trigger-daemon":
                    self._handle_trigger()
                else:
                    self._fail("not found", status_code=404)

            def do_PUT(self):
                path = self.path
                if path == "/config":
                    self._handle_config_put()
                else:
                    self._fail("not found", status_code=404)

            def do_DELETE(self):
                self._fail("not found", status_code=404)

            def do_PATCH(self):
                self._fail("not found", status_code=404)

            def _handle_health(self):
                scanned = outer.stats.get("scanned_feeds", 0)
                self._ok({
                    "status": "ok",
                    "scraper": "running",
                    "scanned_feeds": scanned,
                })

            def _handle_stats(self):
                stats = outer.stats
                feeds_count = stats.get("feeds_count", 0)
                comments_count = stats.get("comments_count", 0)
                if feeds_count == 0:
                    feeds_count = len(getattr(outer.store, "_feed_ids", ()) or [])
                if comments_count == 0:
                    comments_count = len(getattr(outer.store, "_comment_keys", ()) or [])
                self._ok({
                    "feeds_count": feeds_count,
                    "comments_count": comments_count,
                    "media_count": stats.get("media_count", 0),
                    "last_scan_ts": stats.get("last_scan_ts"),
                    "daemon_running": bool(stats.get("daemon_running", False)),
                })

            def _handle_config_get(self):
                self._ok(dict(outer.stats.get("config", {}) or {}))

            def _handle_config_put(self):
                body = self._read_body()
                if body:
                    current = outer.stats.setdefault("config", {})
                    if not isinstance(current, dict):
                        current = {}
                    current.update(body)
                    outer.stats["config"] = current
                self._ok({"updated": True})

            def _handle_logs(self, parsed):
                qs = parse_qs(parsed.query)
                since = self._qs_int(qs, "since", 0)
                max_lines = self._qs_int(qs, "max", 0)
                buf = list(outer.stats.get("log_buffer", []) or [])
                total = len(buf)
                if since < 0:
                    since = 0
                sliced = buf[since:]
                if max_lines and max_lines > 0:
                    sliced = sliced[:max_lines]
                self._ok({"lines": sliced, "total": total})

            def _handle_trigger(self):
                self._read_body()
                cb = outer._trigger_callback
                triggered = False
                if cb is not None:
                    try:
                        cb()
                        triggered = True
                    except Exception as exc:
                        logging.getLogger(__name__).warning(
                            "trigger-daemon callback failed: %s", exc
                        )
                        triggered = False
                self._ok({"triggered": triggered})

            @staticmethod
            def _qs_int(qs, key, default):
                vals = qs.get(key)
                if not vals:
                    return default
                try:
                    return int(vals[0])
                except (ValueError, TypeError):
                    return default

        self._handler_cls = Handler

    def set_trigger_callback(self, callback):
        self._trigger_callback = callback

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
