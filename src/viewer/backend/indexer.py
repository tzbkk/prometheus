"""SQLite indexer — builds feeds/feeds_fts/media tables from JSONL."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

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


class Indexer:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def build_all(
        self, feeds_path: str, media_index_path: str
    ) -> Iterator[float]:
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM feeds_fts")
        conn.execute("DELETE FROM feeds")
        conn.execute("DELETE FROM media")
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
            yield from self._index(
                conn, feeds_path, media_index_path, start_offset=start_offset
            )
        finally:
            conn.close()

    def _load_media_map(
        self, media_index_path: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        # Missing file → empty map so feeds still index without media.
        media_map: Dict[str, List[Dict[str, Any]]] = {}
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
                        media_map.setdefault(source, []).append(entry)
        except FileNotFoundError:
            pass
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
