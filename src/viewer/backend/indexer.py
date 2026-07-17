"""SQLite indexer — builds feeds/feeds_fts/media/comments tables from JSONL.

Multi-guild aware (plan §4.2): per-guild methods keep their 2-arg shape
(``build_all(feeds_path, media_index_path)``) with an optional ``guild_id``
kwarg; new orchestrators (``build_all_guilds(data_dir)`` /
``build_incremental_guilds(data_dir)``) discover guild directories and
loop. Per-guild byte offsets in ``meta`` (keys ``offset:<guild_id>``) fix
the B2 bug where a newly-added guild was skipped forever on a DB that
already had other guilds' rows. After indexing, the SQLite ``guilds``
table is enriched with display names read once from
``conf/guilds.conf.json`` (G28).
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src.viewer.backend.schema import init_db

_BATCH_SIZE = 500


def discover_guilds(data_dir) -> List[Tuple[str, Path]]:
    """Scan ``data_dir/*/feeds.jsonl`` — return ``[(guild_id, dir_path), ...]``.

    G16: shows ALL data dirs regardless of conf — a guild removed from conf
    stays browsable. ``guild_id`` is the subdir basename (numeric only);
    non-numeric dirs (e.g. a stray ``media`` dir at parent level) are skipped.
    """
    result: List[Tuple[str, Path]] = []
    root = Path(data_dir)
    if not root.is_dir():
        return result
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        gid = entry.name
        if not gid.isnumeric():
            continue
        if (entry / "feeds.jsonl").exists():
            result.append((gid, entry))
    return result


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
        self,
        feeds_path: str,
        media_index_path: str,
        guild_id: Optional[str] = None,
    ) -> Iterator[float]:
        """Full rebuild of feeds + media for one guild (or global nuke).

        When ``guild_id`` is None (legacy single-guild mode), the tables are
        wiped globally. When ``guild_id`` is set, only rows for that guild
        are deleted so multi-guild rebuilds don't clobber other guilds.
        """
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        if guild_id is None:
            conn.execute("DELETE FROM media")
            conn.execute("DELETE FROM feeds_fts")
            conn.execute("DELETE FROM feeds")
            conn.execute("DELETE FROM comments")
        else:
            conn.execute(
                "DELETE FROM feeds_fts WHERE feed_id IN "
                "(SELECT id FROM feeds WHERE guild_id = ?)",
                (guild_id,),
            )
            conn.execute("DELETE FROM media WHERE guild_id = ?", (guild_id,))
            conn.execute("DELETE FROM comments WHERE guild_id = ?", (guild_id,))
            conn.execute("DELETE FROM feeds WHERE guild_id = ?", (guild_id,))
        conn.commit()
        try:
            yield from self._index(
                conn, feeds_path, media_index_path, start_offset=0,
                guild_id=guild_id,
            )
            if guild_id is not None:
                self._update_guild_feed_count(conn, guild_id)
        finally:
            conn.close()

    def build_incremental(
        self,
        feeds_path: str,
        media_index_path: str,
        guild_id: Optional[str] = None,
    ) -> Iterator[float]:
        """Incremental index for one guild using byte-offset tracking.

        B2 fix: the offset-0 guard is per-guild (``COUNT(*) FROM feeds WHERE
        guild_id=?``), not global. A newly-added guild (offset=0) on a DB
        with other guilds' rows is now indexed correctly instead of being
        skipped forever.
        """
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            start_offset = self._read_last_offset(conn, guild_id)
            if start_offset <= 0:
                # B2 per-guild guard: skip only if THIS guild already has rows.
                if guild_id is not None:
                    existing = conn.execute(
                        "SELECT COUNT(*) FROM feeds WHERE guild_id = ?",
                        (guild_id,),
                    ).fetchone()[0]
                else:
                    existing = conn.execute(
                        "SELECT COUNT(*) FROM feeds"
                    ).fetchone()[0]
                if existing > 0:
                    yield 100.0
                    return
            yield from self._index(
                conn, feeds_path, media_index_path,
                start_offset=start_offset, guild_id=guild_id,
            )
            if guild_id is not None:
                self._update_guild_feed_count(conn, guild_id)
        finally:
            conn.close()

    def build_all_guilds(self, data_dir) -> Iterator[float]:
        """Full rebuild across ALL discovered guild directories.

        Each guild's rows are deleted per-guild (so a full rebuild doesn't
        lose a guild whose data_dir is temporarily unavailable on the next
        pass). After all guilds are indexed, names are enriched from conf.
        """
        for guild_id, guild_dir in discover_guilds(data_dir):
            feeds_path = str(guild_dir / "feeds.jsonl")
            media_index_path = str(guild_dir / "media_index.jsonl")
            yield from self.build_all(
                feeds_path, media_index_path, guild_id=guild_id,
            )
        self._enrich_guild_names()

    def build_incremental_guilds(self, data_dir) -> Iterator[float]:
        """Incremental index across ALL discovered guild directories."""
        for guild_id, guild_dir in discover_guilds(data_dir):
            feeds_path = str(guild_dir / "feeds.jsonl")
            media_index_path = str(guild_dir / "media_index.jsonl")
            yield from self.build_incremental(
                feeds_path, media_index_path, guild_id=guild_id,
            )
        self._enrich_guild_names()

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
            by_url: Dict[str, Dict[str, Any]] = {}
            for entry in entries:
                url = entry.get("url")
                if not url:
                    continue
                if url not in by_url:
                    by_url[url] = entry
                else:
                    existing = by_url[url].get("file", "")
                    candidate = entry.get("file", "")
                    if "." in candidate and "." not in existing:
                        by_url[url] = entry
            media_map[source] = list(by_url.values())
        return media_map

    def _read_last_offset(
        self, conn: sqlite3.Connection, guild_id: Optional[str] = None,
    ) -> int:
        key = f"offset:{guild_id}" if guild_id else "last_offset"
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        try:
            return int(row[0]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _store_last_offset(
        self,
        conn: sqlite3.Connection,
        offset: int,
        guild_id: Optional[str] = None,
    ) -> None:
        key = f"offset:{guild_id}" if guild_id else "last_offset"
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, str(offset)),
        )

    def _read_comment_offset(
        self, conn: sqlite3.Connection, guild_id: Optional[str] = None,
    ) -> int:
        key = f"comment_offset:{guild_id}" if guild_id else "last_comment_offset"
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        try:
            return int(row[0]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _store_comment_offset(
        self,
        conn: sqlite3.Connection,
        offset: int,
        guild_id: Optional[str] = None,
    ) -> None:
        key = f"comment_offset:{guild_id}" if guild_id else "last_comment_offset"
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, str(offset)),
        )

    def _index(
        self,
        conn: sqlite3.Connection,
        feeds_path: str,
        media_index_path: str,
        start_offset: int,
        guild_id: Optional[str] = None,
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

                # Schema order: id, guild_id, create_time, ... (13 cols).
                feed_rows.append(
                    (
                        feed_id,
                        guild_id,
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
                    # Schema order: feed_id, file, url, type, size, guild_id.
                    media_rows.append(
                        (
                            feed_id,
                            m.get("file"),
                            m.get("url"),
                            m.get("type"),
                            m.get("size"),
                            guild_id,
                        )
                    )

                if len(feed_rows) >= _BATCH_SIZE:
                    self._flush(
                        conn, feed_rows, fts_rows, media_rows, offset,
                        guild_id=guild_id,
                    )
                    yield offset / file_size * 100.0
                    feed_rows.clear()
                    fts_rows.clear()
                    media_rows.clear()

            if feed_rows:
                self._flush(
                    conn, feed_rows, fts_rows, media_rows, offset,
                    guild_id=guild_id,
                )
        yield 100.0

    def _flush(
        self,
        conn: sqlite3.Connection,
        feed_rows: List[tuple],
        fts_rows: List[tuple],
        media_rows: List[tuple],
        offset: int,
        guild_id: Optional[str] = None,
    ) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO feeds "
            "(id, guild_id, create_time, title_text, author_nick, author_id, "
            "author_avatar, like_count, comment_count, image_count, video_count, "
            "raw_json, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            feed_rows,
        )
        if fts_rows:
            conn.executemany(
                "INSERT INTO feeds_fts(feed_id, title_text, raw_json) VALUES (?,?,?)",
                fts_rows,
            )
        if media_rows:
            conn.executemany(
                "INSERT INTO media(feed_id, file, url, type, size, guild_id) "
                "VALUES (?,?,?,?,?,?)",
                media_rows,
            )
        self._store_last_offset(conn, offset, guild_id=guild_id)
        conn.commit()

    def build_comments(
        self,
        comments_path: str,
        guild_id: Optional[str] = None,
    ) -> Iterator[float]:
        """Full rebuild of comments table from comments.jsonl."""
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        if guild_id is None:
            conn.execute("DELETE FROM comments")
        else:
            conn.execute("DELETE FROM comments WHERE guild_id = ?", (guild_id,))
        conn.commit()
        try:
            yield from self._index_comments(
                conn, comments_path, start_offset=0, guild_id=guild_id,
            )
        finally:
            conn.close()

    def build_comments_incremental(
        self,
        comments_path: str,
        guild_id: Optional[str] = None,
    ) -> Iterator[float]:
        """Incremental comment indexing using per-guild byte offsets."""
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            start_offset = self._read_comment_offset(conn, guild_id)
            if start_offset <= 0:
                # Per-guild guard mirrors the B2 feed guard.
                if guild_id is not None:
                    existing = conn.execute(
                        "SELECT COUNT(*) FROM comments WHERE guild_id = ?",
                        (guild_id,),
                    ).fetchone()[0]
                else:
                    existing = conn.execute(
                        "SELECT COUNT(*) FROM comments"
                    ).fetchone()[0]
                if existing > 0:
                    yield 100.0
                    return
            yield from self._index_comments(
                conn, comments_path, start_offset, guild_id=guild_id,
            )
        finally:
            conn.close()

    def _index_comments(
        self,
        conn: sqlite3.Connection,
        comments_path: str,
        start_offset: int,
        guild_id: Optional[str] = None,
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
                    row = self._extract_comment_row(
                        comment, feed_id, parent_id=None, guild_id=guild_id,
                    )
                    if row is not None:
                        comment_rows.append(row)
                    for reply in comment.get("vecReply") or []:
                        reply_row = self._extract_comment_row(
                            reply, feed_id,
                            parent_id=comment.get("id"), guild_id=guild_id,
                        )
                        if reply_row is not None:
                            comment_rows.append(reply_row)

                if len(comment_rows) >= _BATCH_SIZE:
                    self._flush_comments(
                        conn, comment_rows, offset, guild_id=guild_id,
                    )
                    yield offset / file_size * 100.0
                    comment_rows.clear()

            if comment_rows:
                self._flush_comments(
                    conn, comment_rows, offset, guild_id=guild_id,
                )
        yield 100.0

    @staticmethod
    def _extract_comment_row(
        comment: Dict[str, Any],
        feed_id: str,
        parent_id: Optional[str],
        guild_id: Optional[str] = None,
    ) -> Optional[tuple]:
        comment_id = comment.get("id")
        if not comment_id:
            return None
        post_user = comment.get("postUser") or {}
        rich_contents = comment.get("richContents") or {}
        like_info = comment.get("likeInfo") or {}
        # Schema order: id, feed_id, guild_id, parent_id, create_time, ...
        return (
            comment_id,
            feed_id,
            guild_id,
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
        guild_id: Optional[str] = None,
    ) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO comments "
            "(id, feed_id, guild_id, parent_id, create_time, author_nick, "
            "author_avatar, content_text, ip_location, like_count, reply_count, "
            "sequence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            comment_rows,
        )
        self._store_comment_offset(conn, offset, guild_id=guild_id)
        conn.commit()

    def _update_guild_feed_count(
        self, conn: sqlite3.Connection, guild_id: str,
    ) -> None:
        """Cache per-guild feed count + indexed_at into the guilds table.

        Preserves an existing ``guild_number``/``name`` (set earlier by
        ``_enrich_guild_names``) via COALESCE — only feeds/indexed_at are
        refreshed here.
        """
        now = datetime.now(timezone.utc).isoformat()
        count = conn.execute(
            "SELECT COUNT(*) FROM feeds WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO guilds "
            "(guild_id, guild_number, name, feeds, indexed_at) "
            "VALUES (?, "
            "COALESCE((SELECT guild_number FROM guilds WHERE guild_id=?), ''), "
            "COALESCE((SELECT name FROM guilds WHERE guild_id=?), ?), "
            "?, ?)",
            (guild_id, guild_id, guild_id, guild_id, count, now),
        )
        conn.commit()

    def _find_conf_dir(self) -> Optional[Path]:
        """Resolve conf dir for ``guilds.conf.json``.

        Order: ``$PROMETHEUS_CONFIG`` parent → project ``conf/`` (parents[3]
        of this file = project root, since this module lives at
        ``src/viewer/backend/indexer.py``).
        """
        env_path = os.environ.get("PROMETHEUS_CONFIG")
        if env_path:
            p = Path(env_path).resolve()
            if p.is_file():
                return p.parent
            if p.is_dir():
                return p
        project_root = Path(__file__).resolve().parents[3]
        conf_dir = project_root / "conf"
        return conf_dir if conf_dir.is_dir() else None

    def _enrich_guild_names(self) -> None:
        """Read ``conf/guilds.conf.json`` once, upsert name/guild_number.

        G28: the viewer reads guild names from SQLite, NOT from conf at
        runtime — the viewer may run on a different host without conf.
        This populates the SQLite ``guilds`` table at index time only.
        Existing per-guild ``feeds`` counts are preserved via COALESCE so
        callers can run this in any order against ``_update_guild_feed_count``.
        """
        conf_dir = self._find_conf_dir()
        if conf_dir is None:
            return
        guilds_conf = conf_dir / "guilds.conf.json"
        if not guilds_conf.is_file():
            return
        try:
            data = json.loads(guilds_conf.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = datetime.now(timezone.utc).isoformat()
        conn = init_db(self.db_path)
        try:
            for entry in data.get("guilds", []):
                gid = str(entry.get("guild_id", ""))
                if not gid:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO guilds "
                    "(guild_id, guild_number, name, feeds, indexed_at) "
                    "VALUES (?, ?, ?, "
                    "COALESCE((SELECT feeds FROM guilds WHERE guild_id=?), 0), ?)",
                    (
                        gid,
                        entry.get("guild_number"),
                        entry.get("name"),
                        gid,
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
