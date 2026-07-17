"""SQLite indexer — builds feeds/feeds_fts/media/comments tables from JSONL."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from src.web_scraper.urlnorm import normalize_media_url

from src.viewer.backend.schema import init_db

_BATCH_SIZE = 500


def extract_feed_text(raw_json: Dict[str, Any]) -> str:
    # ``title`` is always present (even for image-only posts where ``contents``
    # is empty), making it the reliable source of searchable text.
    title = raw_json.get("title") or {}
    parts: List[str] = []
    for entry in title.get("contents") or []:
        if not isinstance(entry, dict):
            continue
        tc = entry.get("text_content")
        if isinstance(tc, dict):
            text = tc.get("text")
            if text:
                parts.append(text)
    return " ".join(parts)


def extract_author(raw_json: Dict[str, Any]) -> Dict[str, Optional[str]]:
    poster = raw_json.get("poster") or {}
    icon = poster.get("icon") or {}
    return {
        "nick": poster.get("nick"),
        "id": poster.get("id"),
        "avatar": icon.get("iconUrl"),
    }


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_comment_text(rich_contents: Any) -> Optional[str]:
    parts: List[str] = []
    for entry in (rich_contents or {}).get("contents") or []:
        tc = entry.get("text_content")
        if isinstance(tc, dict) and tc.get("text"):
            parts.append(tc["text"])
    return " ".join(parts) if parts else None


class Indexer:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def build_all(
        self, feeds_path: str, media_index_path: str
    ) -> Iterator[float]:
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM media")
        conn.execute("DELETE FROM feeds_fts")
        conn.execute("DELETE FROM feeds")
        conn.execute("DELETE FROM comments")
        conn.commit()
        try:
            yield from self._index(conn, feeds_path, media_index_path, start_offset=0)
        finally:
            conn.close()

    def build_incremental(
        self, feeds_path: str, media_index_path: str
    ) -> Iterator[float]:
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            start_offset = self._read_last_offset(conn)
            # Guard: if start_offset is 0 but DB already has indexed data,
            # treat as already fully indexed (avoid double-indexing).
            if start_offset <= 0:
                existing = conn.execute(
                    "SELECT COUNT(*) FROM feeds"
                ).fetchone()[0]
                if existing > 0:
                    yield 100.0
                    return
            yield from self._index(
                conn, feeds_path, media_index_path, start_offset=start_offset
            )
        finally:
            conn.close()

    def _load_media_map(
        self, media_index_path: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Load media_index.jsonl and deduplicate by (source, url).

        Legacy inject.js wrote extensionless filenames (``abc123``) while
        web_scraper writes ``abc123.jpg``/``abc123.mp4``.  Both append to the
        same file, so the same URL can appear multiple times.  We keep only
        one entry per URL, preferring the one with a file extension so the
        viewer serves a file that actually exists on disk.
        """
        raw_map: Dict[str, List[Dict[str, Any]]] = {}
        try:
            with open(media_index_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    source = entry.get("source")
                    if source:
                        raw_map.setdefault(source, []).append(entry)
        except FileNotFoundError:
            pass

        media_map: Dict[str, List[Dict[str, Any]]] = {}
        for source, entries in raw_map.items():
            best_by_url: Dict[str, Dict[str, Any]] = {}
            for entry in entries:
                norm_url = normalize_media_url(entry.get("url", ""))
                fn = entry.get("file", "")
                existing = best_by_url.get(norm_url)
                if existing is None:
                    best_by_url[norm_url] = entry
                elif "." in fn and "." not in existing.get("file", ""):
                    best_by_url[norm_url] = entry
            media_map[source] = list(best_by_url.values())
        return media_map

    def _read_last_offset(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("last_offset",)
        ).fetchone()
        try:
            return int(row[0]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _store_last_offset(self, conn: sqlite3.Connection, offset: int) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("last_offset", str(offset)),
        )

    def _read_comment_offset(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("last_comment_offset",)
        ).fetchone()
        try:
            return int(row[0]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _store_comment_offset(self, conn: sqlite3.Connection, offset: int) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("last_comment_offset", str(offset)),
        )

    def _index(
        self,
        conn: sqlite3.Connection,
        feeds_path: str,
        media_index_path: str,
        start_offset: int,
    ) -> Iterator[float]:
        media_map = self._load_media_map(media_index_path)
        fts_available = (
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='feeds_fts'"
            ).fetchone()
            is not None
        )
        now = datetime.now(timezone.utc).isoformat()

        feed_rows: List[tuple] = []
        fts_rows: List[tuple] = []
        media_rows: List[tuple] = []
        seen_ids: set = set()

        with open(feeds_path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            f.seek(start_offset)
            if file_size <= start_offset:
                return

            offset = start_offset
            for raw_bytes in f:
                offset = f.tell()
                line = raw_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"indexer: skipping malformed line at byte {offset}: {exc}",
                        file=sys.stderr,
                    )
                    continue

                feed_id = raw.get("id")
                if not feed_id:
                    continue
                if feed_id in seen_ids:
                    continue
                seen_ids.add(feed_id)

                title_text = extract_feed_text(raw)
                author = extract_author(raw)
                create_time = _to_int(raw.get("createTime"), 0) or None
                like_count = _to_int((raw.get("total_like") or {}).get("like_count"), 0)
                comment_count = _to_int(raw.get("commentCount"), 0)
                image_count = len(raw.get("images") or [])
                video_count = len(raw.get("videos") or [])
                raw_json_str = json.dumps(raw, ensure_ascii=False)

                feed_rows.append(
                    (
                        feed_id,
                        create_time,
                        title_text,
                        author["nick"],
                        author["id"],
                        author["avatar"],
                        like_count,
                        comment_count,
                        image_count,
                        video_count,
                        raw_json_str,
                        now,
                    )
                )
                if fts_available:
                    fts_rows.append((feed_id, title_text, raw_json_str))
                for m in media_map.get(feed_id, []):
                    media_rows.append(
                        (
                            feed_id,
                            m.get("file"),
                            m.get("url"),
                            m.get("type"),
                            m.get("size"),
                        )
                    )

                if len(feed_rows) >= _BATCH_SIZE:
                    self._flush(conn, feed_rows, fts_rows, media_rows, offset)
                    yield offset / file_size * 100.0
                    feed_rows.clear()
                    fts_rows.clear()
                    media_rows.clear()

            if feed_rows:
                self._flush(conn, feed_rows, fts_rows, media_rows, offset)
        yield 100.0

    def _flush(
        self,
        conn: sqlite3.Connection,
        feed_rows: List[tuple],
        fts_rows: List[tuple],
        media_rows: List[tuple],
        offset: int,
    ) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO feeds "
            "(id, create_time, title_text, author_nick, author_id, author_avatar, "
            "like_count, comment_count, image_count, video_count, raw_json, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            feed_rows,
        )
        if fts_rows:
            conn.executemany(
                "INSERT INTO feeds_fts(feed_id, title_text, raw_json) VALUES (?,?,?)",
                fts_rows,
            )
        if media_rows:
            conn.executemany(
                "INSERT INTO media(feed_id, file, url, type, size) VALUES (?,?,?,?,?)",
                media_rows,
            )
        self._store_last_offset(conn, offset)
        conn.commit()

    def build_comments(self, comments_path: str) -> Iterator[float]:
        """Full rebuild of comments table from comments.jsonl."""
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM comments")
        conn.commit()
        try:
            yield from self._index_comments(conn, comments_path, start_offset=0)
        finally:
            conn.close()

    def build_comments_incremental(self, comments_path: str) -> Iterator[float]:
        """Incremental comment indexing using byte offset tracking."""
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            start_offset = self._read_comment_offset(conn)
            if start_offset <= 0:
                existing = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
                if existing > 0:
                    yield 100.0
                    return
            yield from self._index_comments(conn, comments_path, start_offset)
        finally:
            conn.close()

    def _index_comments(
        self,
        conn: sqlite3.Connection,
        comments_path: str,
        start_offset: int,
    ) -> Iterator[float]:
        comment_rows: List[tuple] = []

        with open(comments_path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            f.seek(start_offset)
            if file_size <= start_offset:
                return

            offset = start_offset
            for raw_bytes in f:
                offset = f.tell()
                line = raw_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"indexer: skipping malformed comment line at byte {offset}: {exc}",
                        file=sys.stderr,
                    )
                    continue

                if raw.get("_s") != "web_api":
                    continue

                data = raw.get("d") or {}
                feed_id = data.get("feedId")
                if not feed_id:
                    continue

                for comment in data.get("vecComment") or []:
                    row = self._extract_comment_row(comment, feed_id, parent_id=None)
                    if row is not None:
                        comment_rows.append(row)
                    for reply in comment.get("vecReply") or []:
                        reply_row = self._extract_comment_row(
                            reply, feed_id, parent_id=comment.get("id")
                        )
                        if reply_row is not None:
                            comment_rows.append(reply_row)

                if len(comment_rows) >= _BATCH_SIZE:
                    self._flush_comments(conn, comment_rows, offset)
                    yield offset / file_size * 100.0
                    comment_rows.clear()

            if comment_rows:
                self._flush_comments(conn, comment_rows, offset)
        yield 100.0

    @staticmethod
    def _extract_comment_row(
        comment: Dict[str, Any], feed_id: str, parent_id: Optional[str]
    ) -> Optional[tuple]:
        comment_id = comment.get("id")
        if not comment_id:
            return None
        post_user = comment.get("postUser") or {}
        rich_contents = comment.get("richContents") or {}
        like_info = comment.get("likeInfo") or {}
        return (
            comment_id,
            feed_id,
            parent_id,
            _to_int(comment.get("createTime"), 0) or None,
            post_user.get("nick"),
            (post_user.get("icon") or {}).get("iconUrl"),
            _extract_comment_text(rich_contents),
            rich_contents.get("ip_location_province"),
            _to_int(like_info.get("count"), 0),
            _to_int(comment.get("replyCount"), 0),
            _to_int(comment.get("sequence"), 0) or None,
        )

    def _flush_comments(
        self,
        conn: sqlite3.Connection,
        comment_rows: List[tuple],
        offset: int,
    ) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO comments "
            "(id, feed_id, parent_id, create_time, author_nick, author_avatar, "
            "content_text, ip_location, like_count, reply_count, sequence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            comment_rows,
        )
        self._store_comment_offset(conn, offset)
        conn.commit()
