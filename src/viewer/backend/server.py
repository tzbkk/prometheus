"""Viewer backend HTTP server.

Routes ``/api/*`` (T8), ``/media/*`` (T9), and serves SPA static files
otherwise. Binds to 127.0.0.1 only — no CORS, no auth, no WebSocket.

Run with: ``python3 -m src.viewer.backend.server [--port N]``
"""

import argparse
import json
import mimetypes
import os
import signal
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from src.viewer.backend.api import (
    handle_feed_detail,
    handle_feeds,
    handle_rebuild,
    handle_search,
    handle_stats,
)
from src.viewer.backend.indexer import Indexer
from src.viewer.backend.schema import init_db

_DEFAULT_HOST = "127.0.0.1"  # loopback only — never 0.0.0.0
_DEFAULT_PORT = 9422
_DEFAULT_CONFIG = "conf/viewer.conf.json"

# MIME type overrides for asset extensions the stdlib mimetypes table misses
# or returns an obsolete value for (e.g. text/x-js instead of application/javascript).
_EXTRA_MIMETYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_under(base, request_path):
    """Resolve ``request_path`` under ``base``; return None on traversal.

    Leading slashes are stripped and percent escapes decoded before checking.
    Any ``..`` segment or resolved path escaping ``base`` is rejected.
    """
    rel = unquote(request_path.lstrip("/"))
    if "\x00" in rel:
        return None
    parts = rel.split("/")
    if any(seg == ".." for seg in parts):
        return None
    base_resolved = base.resolve()
    full = (base_resolved / rel).resolve()
    try:
        full.relative_to(base_resolved)
    except ValueError:
        return None
    return full


class _ViewerHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying per-server config for the handler.

    Attributes are populated by :class:`ViewerServer` and read by
    :class:`ViewerHandler` via ``self.server``.
    """

    daemon_threads = True
    allow_reuse_address = True

    static_dir: Optional[Path] = None
    data_dir: Optional[Path] = None
    db_path: Optional[str] = None
    db_conn: Optional[sqlite3.Connection] = None


class ViewerHandler(BaseHTTPRequestHandler):
    """Request handler: routes ``/api/*`` and ``/media/*``; serves static files.

    Routing:
      * ``/api/*``    -> API handlers (T8; returns 501 JSON for now).
      * ``/media/*``  -> media file serving (T9; returns 501 JSON for now),
                        with path-traversal prevention already enforced.
      * anything else -> static file from ``static_dir`` with SPA fallback.
    """

    server_version = "PrometheusViewer/1.0"

    def log_message(self, *args, **kwargs):  # noqa: D401 - silence default access log
        pass

    def _send_bytes(self, status_code, content_type, body):
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status_code, envelope):
        payload = json.dumps(envelope).encode("utf-8")
        self._send_bytes(status_code, "application/json; charset=utf-8", payload)

    def _send_text(self, status_code, message):
        self._send_bytes(status_code, "text/plain; charset=utf-8", message.encode("utf-8"))

    def _route(self):
        path = urlparse(self.path).path or "/"
        if path.startswith("/api/"):
            self._route_api(path)
        elif path.startswith("/media/"):
            self._route_media(path)
        else:
            self._route_static(path)

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        path = urlparse(self.path).path or "/"
        if path.startswith("/api/"):
            self._route_api(path)
        else:
            self._send_text(404, "not found")

    def do_PUT(self):
        path = urlparse(self.path).path or "/"
        if path.startswith("/api/"):
            self._route_api(path)
        else:
            self._send_text(404, "not found")

    def do_DELETE(self):
        path = urlparse(self.path).path or "/"
        if path.startswith("/api/"):
            self._route_api(path)
        else:
            self._send_text(404, "not found")

    def do_PATCH(self):
        self._send_text(404, "not found")

    def _route_api(self, path):
        db_path = getattr(self.server, "db_path", None)
        if db_path is None:
            self._send_json(500, {"error": "database not configured"})
            return

        query_params = parse_qs(urlparse(self.path).query)

        if path == "/api/feeds":
            status, body = handle_feeds(db_path, query_params)
        elif path.startswith("/api/feed/"):
            feed_id = unquote(path[len("/api/feed/"):])
            status, body = handle_feed_detail(db_path, feed_id)
        elif path == "/api/search":
            status, body = handle_search(db_path, query_params)
        elif path == "/api/stats":
            status, body = handle_stats(db_path)
        elif path == "/api/rebuild" and self.command == "POST":
            data_dir = getattr(self.server, "data_dir", None)
            if data_dir is None:
                self._send_json(500, {"error": "data_dir not configured"})
                return
            status, body = handle_rebuild(db_path, str(data_dir))
        else:
            self._send_json(501, {"ok": False, "data": {}, "error": "not implemented"})
            return

        self._send_json(status, body)

    def _route_media(self, path):
        filename = path[len("/media/"):]
        if not self._is_safe_media_name(filename):
            self._send_text(403, "forbidden")
            return
        data_dir = getattr(self.server, "data_dir", None)
        if data_dir is None:
            self._send_text(500, "data dir not configured")
            return
        media_dir = (data_dir / "media").resolve()
        full = (media_dir / filename).resolve()
        try:
            full.relative_to(media_dir)
        except ValueError:
            self._send_text(403, "forbidden")
            return
        if not full.is_file():
            self._send_text(404, "not found")
            return
        self._serve_media(full)

    @staticmethod
    def _is_safe_media_name(name):
        """A safe media filename has no path separators, parent refs, or NULs.

        The spec calls for rejecting any ``..`` or ``/`` in the filename
        following ``/media/`` — a media reference is a bare filename, not a
        subpath.
        """
        if not name:
            return False
        if "/" in name or "\\" in name or ".." in name:
            return False
        if "\x00" in name:
            return False
        return True

    _MEDIA_MIMETYPES = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }

    _STREAM_CHUNK = 64 * 1024

    def _serve_media(self, file_path):
        try:
            size = file_path.stat().st_size
        except OSError:
            self._send_text(404, "not found")
            return
        ctype = self._MEDIA_MIMETYPES.get(file_path.suffix.lower(),
                                         "application/octet-stream")
        range_header = self.headers.get("Range")
        if range_header:
            span = self._parse_range(range_header, size)
            if span is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = span
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if self.command != "HEAD":
                self._stream_file(file_path, start, length)
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if self.command != "HEAD":
                self._stream_file(file_path, 0, size)

    @staticmethod
    def _parse_range(header, size):
        """Parse a ``bytes=...`` Range header; return (start, end) inclusive
        or None if unsatisfiable. Malformed headers return None so the caller
        emits 416. Only a single range is honored; ``bytes=a,b`` is rejected.
        """
        spec = header.strip()
        if not spec.startswith("bytes="):
            return None
        body = spec[len("bytes="):].strip()
        if "," in body:
            return None
        if "=" in body:
            return None
        if "-" not in body:
            return None
        start_str, end_str = body.split("-", 1)
        start_str = start_str.strip()
        end_str = end_str.strip()
        try:
            if start_str == "":
                if end_str == "":
                    return None
                suffix = int(end_str)
                if suffix <= 0:
                    return None
                if suffix > size:
                    suffix = size
                start = size - suffix
                end = size - 1
            else:
                start = int(start_str)
                if end_str == "":
                    end = size - 1
                else:
                    end = int(end_str)
                if start < 0 or start >= size:
                    return None
                if end >= size:
                    end = size - 1
        except ValueError:
            return None
        if start > end:
            return None
        return start, end

    def _stream_file(self, file_path, offset, length):
        remaining = length
        with open(file_path, "rb") as f:
            f.seek(offset)
            while remaining > 0:
                chunk = f.read(min(self._STREAM_CHUNK, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    return
                remaining -= len(chunk)

    def _route_static(self, path):
        static_base = getattr(self.server, "static_dir", None)
        if static_base is None:
            self._send_text(404, "static dir not configured")
            return
        full = _resolve_under(static_base, path)
        if full is None:
            self._send_text(403, "forbidden")
            return
        if full.is_file():
            self._serve_file(full)
            return
        # SPA fallback: extension-less paths (client-side routes) serve index.html.
        # Missing assets (e.g. /assets/missing.js) still 404.
        if not full.suffix:
            index = static_base / "index.html"
            if index.is_file():
                self._serve_file(index)
                return
        self._send_text(404, "not found")

    def _serve_file(self, file_path):
        try:
            data = file_path.read_bytes()
        except OSError:
            self._send_text(404, "not found")
            return
        ctype, _ = mimetypes.guess_type(str(file_path))
        ext = file_path.suffix.lower()
        if ext in _EXTRA_MIMETYPES:
            ctype = _EXTRA_MIMETYPES[ext]
        if ctype is None:
            ctype = "application/octet-stream"
        self._send_bytes(200, ctype, data)


class ViewerServer:
    """Wraps :class:`ThreadingHTTPServer` bound to loopback with viewer config.

    The host is fixed to 127.0.0.1 — the viewer is same-origin only and must
    never bind to 0.0.0.0.
    """

    def __init__(self, host=_DEFAULT_HOST, port=_DEFAULT_PORT,
                 static_dir=None, data_dir=None, db_path=None):
        self.host = host
        self.requested_port = port
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.data_dir = Path(data_dir).resolve() if data_dir else None
        self.db_path = db_path
        self.db_conn = None
        if db_path:
            # Ensure schema exists; connection reused by T8 query handlers.
            self.db_conn = init_db(db_path)
        self.httpd = _ViewerHTTPServer((host, port), ViewerHandler)
        self.httpd.static_dir = self.static_dir
        self.httpd.data_dir = self.data_dir
        self.httpd.db_path = self.db_path
        self.httpd.db_conn = self.db_conn
        # Reflect the actually bound port (supports port=0 for OS-assigned).
        self.port = self.httpd.server_address[1]
        self._shutdown_lock = threading.Lock()
        self._closed = False

    def serve_forever(self):
        self.httpd.serve_forever()

    def shutdown(self):
        with self._shutdown_lock:
            if self._closed:
                return
            self._closed = True
            self.httpd.shutdown()
            self.httpd.server_close()
            if self.db_conn is not None:
                self.db_conn.close()
                self.db_conn = None

    @property
    def server_address(self):
        return self.httpd.server_address


def _install_signal_handlers(shutdown_event):
    """SIGTERM/SIGINT set ``shutdown_event`` from the main thread.

    The event interrupts ``shutdown_event.wait()`` in :func:`main`; the main
    thread then calls ``server.shutdown()`` cleanly. Calling shutdown from
    within the signal handler itself deadlocks because ``serve_forever`` runs
    in the same thread that receives the signal.
    """

    def _handle(signum, frame):  # noqa: ARG001
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Prometheus viewer backend server",
    )
    parser.add_argument("--config", default=_DEFAULT_CONFIG,
                        help="path to viewer config JSON (default: %(default)s)")
    parser.add_argument("--port", type=int, default=None,
                        help="override config port")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    port = args.port if args.port is not None else int(cfg.get("port", _DEFAULT_PORT))
    host = _DEFAULT_HOST
    static_dir = cfg.get("static_dir", "src/viewer/static")
    data_dir = cfg.get("data_dir", "data")
    db_path = cfg.get("db_path", "db/viewer.db")

    feeds_path = os.path.join(data_dir, "feeds.jsonl")
    media_index_path = os.path.join(data_dir, "media_index.jsonl")
    indexer = Indexer(db_path)
    for _ in indexer.build_incremental(feeds_path, media_index_path):
        pass

    poll_interval = int(cfg.get("poll_interval", 30))
    poll_stop: threading.Event | None = None
    poll_thread: threading.Thread | None = None
    if poll_interval > 0:
        poll_stop = threading.Event()

        def _poll_index():
            while not poll_stop.wait(poll_interval):
                for _ in indexer.build_incremental(feeds_path, media_index_path):
                    pass

        poll_thread = threading.Thread(target=_poll_index, daemon=True)
        poll_thread.start()

    def _cleanup():
        if poll_stop is not None:
            poll_stop.set()
        if poll_thread is not None:
            poll_thread.join(timeout=2)

    server = ViewerServer(
        host=host,
        port=port,
        static_dir=static_dir,
        data_dir=data_dir,
        db_path=db_path,
    )

    shutdown_event = threading.Event()
    _install_signal_handlers(shutdown_event)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(
        f"Prometheus viewer on http://{server.host}:{server.port} "
        f"(static: {server.static_dir})",
        flush=True,
    )

    shutdown_event.wait()
    _cleanup()
    server.shutdown()
    server_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
