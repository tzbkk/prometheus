"""Viewer backend API handlers — feeds, detail, search, stats, rebuild.

Each handler is a pure function taking the db path (and request parameters)
and returning ``(status_code, body)`` where ``body`` is JSON-serializable.
The caller (:meth:`ViewerHandler._route_api`) sends the JSON response.

Response contracts mirror ``src/viewer/frontend/src/lib/api.ts`` (source of
truth): list endpoints return bare JSON arrays; detail/stats/rebuild return
bare JSON objects. No outer ``{ok, data}`` envelope — the HTTP status code
carries success/failure, matching the TS ``request()`` helper which throws
on non-2xx.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from src.viewer.backend.indexer import Indexer

ResponseBody = Union[List[Any], Dict[str, Any]]
HandlerResult = Tuple[int, ResponseBody]

# Must mirror the TS Feed interface in src/viewer/frontend/src/lib/api.ts —
# the frontend reads these keys by name.
_FEED_COLUMNS = (
    "id, create_time, title_text, author_nick, author_id, author_avatar, "
    "like_count, comment_count, image_count, video_count"
)


def _get_param(params: Dict[str, List[str]], name: str,
               default: Optional[str] = None) -> Optional[str]:
    values = params.get(name)
    if values:
        return values[0]
    return default


def _parse_pagination(params: Dict[str, List[str]]) -> Tuple[int, int]:
    """Parse and validate ``page`` / ``size`` query parameters.

    Raises ``ValueError`` with a human-readable message on invalid input;
    the caller maps that to HTTP 400.
    """
    page_str = _get_param(params, "page", "1")
    size_str = _get_param(params, "size", "20")
    try:
        page = int(page_str)  # type: ignore[arg-type]
        size = int(size_str)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("page and size must be integers")
    if page < 1:
        raise ValueError("page must be >= 1")
    if size < 1 or size > 100:
        raise ValueError("size must be between 1 and 100")
    return page, size


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def handle_feeds(db_path: str, query_params: Dict[str, List[str]]) -> HandlerResult:
    """GET /api/feeds?page=&size= — newest-first paginated feed list.

    Each item includes all ``Feed`` columns plus ``first_media`` (the file
    name of the first media row, or null) for thumbnail display.
    """
    try:
        page, size = _parse_pagination(query_params)
    except ValueError as exc:
        return 400, {"error": str(exc)}

    offset = (page - 1) * size
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT {_FEED_COLUMNS}, "
            "(SELECT file FROM media WHERE feed_id = feeds.id "
            "AND url NOT LIKE '%qlogo%' ORDER BY rowid LIMIT 1) AS first_media "
            "FROM feeds ORDER BY create_time DESC LIMIT ? OFFSET ?",
            (size, offset),
        ).fetchall()
    finally:
        conn.close()
    return 200, [_row_to_dict(r) for r in rows]


def handle_feed_comments(db_path: str, feed_id: str) -> HandlerResult:
    """GET /api/feed/<id>/comments — list comments for a feed."""
    if not feed_id:
        return 404, {"error": "not found"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, create_time, author_nick, author_avatar, "
            "content_text, ip_location, like_count, reply_count, "
            "parent_id, sequence "
            "FROM comments WHERE feed_id = ? ORDER BY sequence",
            (feed_id,),
        ).fetchall()
    finally:
        conn.close()
    return 200, [_row_to_dict(r) for r in rows]


def handle_feed_detail(db_path: str, feed_id: str) -> HandlerResult:
    """GET /api/feed/<id> — single feed with parsed raw_json and media list."""
    if not feed_id:
        return 404, {"error": "not found"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT {_FEED_COLUMNS}, raw_json, indexed_at FROM feeds WHERE id = ?",
            (feed_id,),
        ).fetchone()
        if row is None:
            return 404, {"error": "not found"}
    finally:
        conn.close()

    feed = _row_to_dict(row)
    raw_json_str = feed.pop("raw_json", None)
    try:
        raw = json.loads(raw_json_str) if raw_json_str else None
    except (json.JSONDecodeError, TypeError):
        raw = None
    feed["raw_json"] = raw

    original_urls: set[str] = set()
    if raw:
        for img in raw.get("images", []) or []:
            u = img.get("picUrl")
            if u:
                original_urls.add(u)
        for vid in raw.get("videos", []) or []:
            u = vid.get("videoUrl") or vid.get("picUrl")
            if u:
                original_urls.add(u)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if original_urls:
            placeholders = ",".join("?" * len(original_urls))
            media_rows = conn.execute(
                f"SELECT file, url, type, size FROM media "
                f"WHERE feed_id = ? AND url IN ({placeholders}) "
                "AND url NOT LIKE '%qlogo%' ORDER BY rowid",
                (feed_id, *original_urls),
            ).fetchall()
        else:
            media_rows = conn.execute(
                "SELECT file, url, type, size FROM media "
                "WHERE feed_id = ? AND url NOT LIKE '%qlogo%' ORDER BY rowid",
                (feed_id,),
            ).fetchall()
    finally:
        conn.close()

    feed["media"] = [_row_to_dict(m) for m in media_rows]
    return 200, feed


def handle_search(db_path: str, query_params: Dict[str, List[str]]) -> HandlerResult:
    """GET /api/search?q=&page=&size= — substring search on title_text.

    Uses LIKE instead of FTS5 MATCH because FTS5's ``unicode61`` tokenizer
    does not segment CJK text, making Chinese substring searches useless.
    With ~8745 feeds, LIKE performance is perfectly adequate.
    """
    q = _get_param(query_params, "q", "") or ""
    if not q.strip():
        return 400, {"error": "q parameter is required"}
    try:
        page, size = _parse_pagination(query_params)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    offset = (page - 1) * size

    pattern = f"%{q}%"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT {_FEED_COLUMNS} FROM feeds "
            "WHERE title_text LIKE ? OR raw_json LIKE ? "
            "ORDER BY create_time DESC LIMIT ? OFFSET ?",
            (pattern, pattern, size, offset),
        ).fetchall()
    finally:
        conn.close()
    return 200, [_row_to_dict(r) for r in rows]


def handle_stats(db_path: str) -> HandlerResult:
    """GET /api/stats — ingestion summary counts and DB metadata.

    Includes keys under two naming conventions: the snake_case names from the
    task spec (``feed_count`` etc.) and the TS ``Stats`` interface names
    (``total_feeds`` etc.) so both the curl verification and the React
    frontend are satisfied.
    """
    conn = sqlite3.connect(db_path)
    try:
        feed_count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        media_count = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        meta_row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("indexed_at",)
        ).fetchone()
    finally:
        conn.close()

    last_indexed = meta_row[0] if meta_row else None
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    return 200, {
        "feed_count": feed_count,
        "media_count": media_count,
        "db_size": db_size,
        "last_indexed": last_indexed,
        # Aliases matching the TS Stats interface (api.ts source of truth).
        "total_feeds": feed_count,
        "total_media": media_count,
        "last_indexed_at": last_indexed,
    }


def handle_rebuild(db_path: str, data_dir: str) -> HandlerResult:
    """POST /api/rebuild — full index rebuild from scratch.

    Drops all feeds, FTS, and media rows, then re-indexes feeds.jsonl and
    media_index.jsonl from the beginning. Use when the index is corrupted or
    after bulk data cleanup.
    """
    feeds_path = os.path.join(str(data_dir), "feeds.jsonl")
    media_index_path = os.path.join(str(data_dir), "media_index.jsonl")

    if not os.path.exists(feeds_path):
        return 404, {"ok": False, "error": f"feeds file not found: {feeds_path}"}

    conn = sqlite3.connect(db_path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
    finally:
        conn.close()

    indexer = Indexer(db_path)
    try:
        list(indexer.build_all(feeds_path, media_index_path))
    except Exception as exc:
        return 500, {"ok": False, "error": str(exc)}

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        after = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("indexed_at", now),
        )
        conn.commit()
    finally:
        conn.close()

    return 200, {"ok": True, "new_count": after - before, "total": after}
