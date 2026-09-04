"""Viewer backend HTTP server — 契约面。

路由 ``/api/*``（六指令边，api.py）、``/media/<guild>/<shard>/<file>``（媒体树
三段路径 + Range 服务——二进制不入语料、行为测试覆盖）、其余 SPA 静态文件。
Binds 127.0.0.1 only — no CORS, no auth, no WebSocket。

Run with: ``python -m src.viewer.backend.server [--port N]``（契约 Runs 面——
conf 缺失时按内置默认起，裸环境可运行）。
"""

import argparse
import json
import mimetypes
import signal
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from src.entity_store.paths import PathFormatError, resolve as resolve_entity
from src.viewer.backend.api import (
    error_envelope,
    handle_feed_comments,
    handle_feed_detail,
    handle_feeds,
    handle_guilds,
    handle_search,
    handle_stats,
    media_indexed,
)
from src.viewer.backend.indexer import Indexer
from src.viewer.backend.schema import init_db

_DEFAULT_HOST = "127.0.0.1"  # loopback only — never 0.0.0.0
_DEFAULT_PORT = 9422
_DEFAULT_CONFIG = "conf/viewer.conf.json"

_KNOWN_API_ROUTES = frozenset(
    {"/api/feeds", "/api/search", "/api/guilds", "/api/stats", "/api/rebuild"}
)

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


def load_config(config_path: str) -> dict:
    """conf 容缺省（契约 Runs = python -m src.viewer.backend.server 裸环境可起）。"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_under(base, request_path):
    """Resolve ``request_path`` under ``base``; return None on traversal."""
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
    """ThreadingHTTPServer carrying per-server config for the handler."""

    daemon_threads = True
    allow_reuse_address = True

    static_dir: Optional[Path] = None
    data_dir: Optional[Path] = None
    db_path: Optional[str] = None
    db_conn: Optional[sqlite3.Connection] = None
    rebuild_lock: Optional[threading.Lock] = None

    def request_rebuild(self) -> bool:
        """受理异步全量重建（幂等：已在跑亦受理——RebuildAck accepted 语义）。

        重建线程与周期 poller 共用 rebuild_lock 串行化（查询面只见 per-guild
        DELETE+INSERT 的提交态）。_rebuild_thread 赋值的竞态最坏并发两线程，
        lock 保证正确性——受理语义不要求单例。
        """
        thread = self._rebuild_thread
        if thread is not None and thread.is_alive():
            return True

        def _run():
            data_dir = self.data_dir
            db_path = self.db_path
            if data_dir is None or not db_path:
                return
            with self.rebuild_lock:
                Indexer(db_path).rebuild_all(data_dir)

        self._rebuild_thread = threading.Thread(
            target=_run, name="viewer-rebuild", daemon=True
        )
        self._rebuild_thread.start()
        return True

    _rebuild_thread: Optional[threading.Thread] = None


class ViewerHandler(BaseHTTPRequestHandler):
    """Request handler: routes ``/api/*`` and ``/media/*``; serves static files."""

    server_version = "PrometheusViewer/2.0"

    def log_message(self, *args, **kwargs):  # noqa: D401 - silence default access log
        pass

    def _send_bytes(self, status_code, content_type, body):
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status_code, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status_code, "application/json; charset=utf-8", payload)

    def _error(self, status_code, code, message):
        self._send_json(status_code, error_envelope(code, message))

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
            self._error(500, "db_not_configured", "viewer database is not configured")
            return

        query_params = parse_qs(urlparse(self.path).query)
        method = self.command

        if path == "/api/feeds" and method == "GET":
            status, body = handle_feeds(db_path, query_params)
        elif path == "/api/search" and method == "GET":
            status, body = handle_search(db_path, query_params)
        elif path == "/api/guilds" and method == "GET":
            status, body = handle_guilds(db_path)
        elif path == "/api/stats" and method == "GET":
            status, body = handle_stats(db_path)
        elif path.startswith("/api/feed/") and method == "GET":
            rest = unquote(path[len("/api/feed/"):])
            if rest.endswith("/comments"):
                status, body = handle_feed_comments(db_path, rest[: -len("/comments")])
            else:
                status, body = handle_feed_detail(db_path, rest)
        elif path == "/api/rebuild" and method == "POST":
            if getattr(self.server, "data_dir", None) is None:
                self._error(
                    500, "data_dir_not_configured", "viewer data_dir is not configured"
                )
                return
            self.server.request_rebuild()
            status, body = 200, {"accepted": True}
        else:
            if path in _KNOWN_API_ROUTES or path.startswith("/api/feed/"):
                self._error(
                    405, "method_not_allowed", f"{method} is not allowed on {path}"
                )
            else:
                self._error(404, "not_found", f"unknown api route: {path}")
            return

        self._send_json(status, body)

    def _route_media(self, path):
        """媒体树：/media/<guild>/<shard>/<file>——恰好 3 段（canonical 全路径）。

        契约语法（MediaAsset grammar：64-hex 名 + 桶 = 名前 2 位）经
        paths.resolve fail-loud 校验；穿越经 relative_to 防护。
        两段形态 /media/<guild>/<file> 为兼容路由（二进制面行为层
        自由）：经 SQLite 媒体索引反查
        分片路径后同款服务；三段 canonical 不动。
        """
        rel = unquote(path[len("/media/"):])
        parts = rel.split("/")
        if len(parts) == 2 and all(parts):
            self._route_media_two_segment(parts[0], parts[1])
            return
        if len(parts) != 3 or not all(parts):
            self._send_text(404, "not found")
            return
        guild_id, shard, filename = parts
        if not guild_id.isnumeric():
            self._send_text(404, "not found")
            return

        data_dir = getattr(self.server, "data_dir", None)
        if data_dir is None:
            self._send_text(500, "data dir not configured")
            return
        guild_media_dir = (data_dir / guild_id / "media").resolve()
        full = (guild_media_dir / shard / filename).resolve()
        try:
            full.relative_to(guild_media_dir)
            resolve_entity(full)  # shard 2-hex + 文件名 grammar + 桶↔名一致性
        except (ValueError, PathFormatError):
            self._send_text(403, "forbidden")
            return
        if not full.is_file():
            self._send_text(404, "not found")
            return
        self._serve_media(full)

    def _route_media_two_segment(self, guild_id: str, filename: str):
        """两段兼容路由：/media/<guild>/<file> ——索引反查分片路径后同款文件服务。

        file 名自述分片（内容寻址，桶 = 名前 2 位），但反查经 SQLite 媒体索引
        （media ∪ comment_media，api.media_indexed）——两段 URL 只解析已索引
        媒体。未命中/畸形/非数字 guild → 404 ErrorEnvelope（二进制面行为层）；
        命中 → _serve_media（含 Range 206）。
        """
        if not guild_id.isnumeric():
            self._error(404, "not_found", f"unknown guild {guild_id!r}")
            return
        db_path = getattr(self.server, "db_path", None)
        data_dir = getattr(self.server, "data_dir", None)
        if db_path is None or data_dir is None:
            self._error(500, "media_unavailable", "viewer db/data_dir is not configured")
            return
        shard = filename[:2]
        full = (data_dir / guild_id / "media" / shard / filename).resolve()
        try:
            resolve_entity(full)  # grammar + 桶↔名一致性 fail-loud
        except PathFormatError:
            self._error(404, "not_found", f"malformed media name {filename!r}")
            return
        if not media_indexed(db_path, guild_id, filename):
            self._error(404, "not_found", f"media {filename!r} is not indexed")
            return
        if not full.is_file():
            self._error(404, "not_found", "media file missing on disk")
            return
        self._serve_media(full)

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
    never bind to 0.0.0.0. 索引不在构造器内跑（被动形态——启动索引归 main()，
    测试自控；db_path-only 构造照常可用）。
    """

    def __init__(self, host=_DEFAULT_HOST, port=_DEFAULT_PORT,
                 static_dir=None, data_dir=None, db_path=None,
                 rebuild_lock=None):
        self.host = host
        self.requested_port = port
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.data_dir = Path(data_dir).resolve() if data_dir else None
        self.db_path = db_path
        self.db_conn = None
        if db_path:
            self.db_conn = init_db(db_path)
        self.rebuild_lock = rebuild_lock if rebuild_lock is not None else threading.Lock()
        self.httpd = _ViewerHTTPServer((host, port), ViewerHandler)
        self.httpd.static_dir = self.static_dir
        self.httpd.data_dir = self.data_dir
        self.httpd.db_path = self.db_path
        self.httpd.db_conn = self.db_conn
        self.httpd.rebuild_lock = self.rebuild_lock
        self.port = self.httpd.server_address[1]
        self._shutdown_lock = threading.Lock()
        self._closed = False

    def request_rebuild(self) -> bool:
        """异步全量重建受理（转 httpd——线程/锁驻服务对象）。"""
        return self.httpd.request_rebuild()

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
    """SIGTERM/SIGINT set ``shutdown_event`` from the main thread."""

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
    poll_interval = int(cfg.get("poll_interval", 30))

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    rebuild_lock = threading.Lock()

    # 启动全量索引（同步——首请求即可见）；poller 周期性全量重建
    # （全量重建策略，~秒级/43k 实体）。
    with rebuild_lock:
        Indexer(db_path).rebuild_all(data_dir)

    poll_stop: threading.Event | None = None
    poll_thread: threading.Thread | None = None
    if poll_interval > 0:
        poll_stop = threading.Event()

        def _poll_index():
            while not poll_stop.wait(poll_interval):
                with rebuild_lock:
                    Indexer(db_path).rebuild_all(data_dir)

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
        rebuild_lock=rebuild_lock,
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
