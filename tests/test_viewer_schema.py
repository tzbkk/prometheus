"""Tests for src/viewer/backend/schema.py — SQLite schema + init_db.

Run with:
    python3 -m pytest tests/test_viewer_schema.py -v
or:
    python3 -m unittest tests.test_viewer_schema -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.viewer.backend.schema import fts5_available, init_db  # noqa: E402


def _table_names(conn):
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }


def _index_names(conn):
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }


class TestFts5Availability(unittest.TestCase):
    def test_fts5_available_returns_true_on_modern_sqlite(self):
        conn = sqlite3.connect(":memory:")
        try:
            if not fts5_available(conn):
                self.skipTest("SQLite build has no FTS5")
            self.assertTrue(fts5_available(conn))
        finally:
            conn.close()

    def test_fts5_available_probe_cleans_up(self):
        conn = sqlite3.connect(":memory:")
        try:
            fts5_available(conn)
            self.assertNotIn("__fts5_probe", _table_names(conn))
        finally:
            conn.close()


class TestInitDbCreatesAllTables(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_creates_feeds_table(self):
        self.assertIn("feeds", _table_names(self.conn))

    def test_creates_media_table(self):
        self.assertIn("media", _table_names(self.conn))

    def test_creates_meta_table(self):
        self.assertIn("meta", _table_names(self.conn))

    def test_creates_feeds_fts_when_supported(self):
        if not fts5_available(sqlite3.connect(":memory:")):
            self.skipTest("SQLite build has no FTS5")
        self.assertIn("feeds_fts", _table_names(self.conn))

    def test_creates_indexes(self):
        indexes = _index_names(self.conn)
        self.assertIn("idx_feeds_create_time", indexes)
        self.assertIn("idx_media_feed_id", indexes)


class TestFeedsColumns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_feeds_has_expected_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(feeds)").fetchall()}
        expected = {
            "id", "guild_id", "create_time", "title_text", "author_nick", "author_id",
            "author_avatar", "like_count", "comment_count", "image_count",
            "video_count", "raw_json", "indexed_at",
        }
        self.assertEqual(cols, expected)

    def test_media_has_expected_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(media)").fetchall()}
        self.assertEqual(cols, {"feed_id", "file", "url", "type", "size", "guild_id"})

    def test_comments_has_expected_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(comments)").fetchall()}
        expected = {
            "id", "feed_id", "guild_id", "parent_id", "create_time", "author_nick",
            "author_avatar", "content_text", "ip_location", "like_count",
            "reply_count", "sequence",
        }
        self.assertEqual(cols, expected)

    def test_meta_has_expected_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(meta)").fetchall()}
        self.assertEqual(cols, {"key", "value"})


class TestInitDbIsIdempotent(unittest.TestCase):
    def test_calling_twice_does_not_error(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name
        try:
            c1 = init_db(db_path)
            c1.close()
            c2 = init_db(db_path)
            try:
                self.assertIn("feeds", _table_names(c2))
            finally:
                c2.close()
        finally:
            os.unlink(db_path)


class TestFtsSearchWorks(unittest.TestCase):
    def test_fts5_match_returns_inserted_row(self):
        """unicode61 splits on whitespace/punctuation; querying a token hits."""
        if not fts5_available(sqlite3.connect(":memory:")):
            self.skipTest("SQLite build has no FTS5")
        conn = init_db(":memory:")
        try:
            conn.execute(
                "INSERT INTO feeds_fts(feed_id, title_text, raw_json) VALUES (?,?,?)",
                ("B_1", "出日版 manga volumes", "{}"),
            )
            conn.commit()
            hit = conn.execute(
                "SELECT feed_id FROM feeds_fts WHERE feeds_fts MATCH ?",
                ("manga",),
            ).fetchone()
            self.assertEqual(hit, ("B_1",))
        finally:
            conn.close()


class TestMetaKeyValue(unittest.TestCase):
    def test_can_store_and_read_last_offset(self):
        conn = init_db(":memory:")
        try:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?,?)",
                ("last_offset", "12345"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", ("last_offset",)
            ).fetchone()
            self.assertEqual(row, ("12345",))
        finally:
            conn.close()


# DDL mirroring the pre-multi-guild schema (no guild_id columns). Used to
# simulate a legacy DB file for the upgrade-path test.
_LEGACY_FEEDS_DDL = """
CREATE TABLE feeds (
    id             TEXT PRIMARY KEY,
    create_time    INTEGER,
    title_text     TEXT,
    author_nick    TEXT,
    author_id      TEXT,
    author_avatar  TEXT,
    like_count     INTEGER DEFAULT 0,
    comment_count  INTEGER DEFAULT 0,
    image_count    INTEGER DEFAULT 0,
    video_count    INTEGER DEFAULT 0,
    raw_json       TEXT,
    indexed_at     TEXT
)
"""
_LEGACY_COMMENTS_DDL = """
CREATE TABLE comments (
    id             TEXT PRIMARY KEY,
    feed_id        TEXT NOT NULL,
    parent_id      TEXT,
    create_time    INTEGER,
    author_nick    TEXT,
    author_avatar  TEXT,
    content_text   TEXT,
    ip_location    TEXT,
    like_count     INTEGER DEFAULT 0,
    reply_count    INTEGER DEFAULT 0,
    sequence       INTEGER
)
"""
_LEGACY_MEDIA_DDL = """
CREATE TABLE media (
    feed_id  TEXT,
    file     TEXT,
    url      TEXT,
    type     TEXT,
    size     INTEGER,
    FOREIGN KEY (feed_id) REFERENCES feeds(id)
)
"""


class TestMultiGuildSchema(unittest.TestCase):
    def test_guild_id_columns_exist(self):
        conn = init_db(":memory:")
        try:
            feeds_cols = {r[1] for r in conn.execute("PRAGMA table_info(feeds)").fetchall()}
            comments_cols = {r[1] for r in conn.execute("PRAGMA table_info(comments)").fetchall()}
            media_cols = {r[1] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
            self.assertIn("guild_id", feeds_cols)
            self.assertIn("guild_id", comments_cols)
            self.assertIn("guild_id", media_cols)
        finally:
            conn.close()

    def test_guilds_table_exists(self):
        conn = init_db(":memory:")
        try:
            self.assertIn("guilds", _table_names(conn))
            cols = {r[1] for r in conn.execute("PRAGMA table_info(guilds)").fetchall()}
            expected = {"guild_id", "guild_number", "name", "feeds", "indexed_at"}
            self.assertEqual(cols, expected)
        finally:
            conn.close()

    def test_indexes_exist(self):
        conn = init_db(":memory:")
        try:
            indexes = _index_names(conn)
            self.assertIn("idx_feeds_guild_id", indexes)
            self.assertIn("idx_media_guild_id", indexes)
        finally:
            conn.close()

    def test_upgrade_adds_guild_id_to_existing_db(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name
        try:
            legacy = sqlite3.connect(db_path)
            legacy.execute(_LEGACY_FEEDS_DDL)
            legacy.execute(_LEGACY_COMMENTS_DDL)
            legacy.execute(_LEGACY_MEDIA_DDL)
            legacy.execute(
                "INSERT INTO feeds(id, create_time, title_text, raw_json, indexed_at) "
                "VALUES (?,?,?,?,?)",
                ("legacy_1", 1700000000, "pre-migration row", "{}", "2024-01-01T00:00:00Z"),
            )
            legacy.execute(
                "INSERT INTO comments(id, feed_id, content_text) VALUES (?,?,?)",
                ("c1", "legacy_1", "hello"),
            )
            legacy.execute(
                "INSERT INTO media(feed_id, file, url) VALUES (?,?,?)",
                ("legacy_1", "img.jpg", "http://example/img.jpg"),
            )
            legacy.commit()
            legacy.close()

            pre = sqlite3.connect(db_path)
            pre_feeds = {r[1] for r in pre.execute("PRAGMA table_info(feeds)").fetchall()}
            pre.close()
            self.assertNotIn("guild_id", pre_feeds)

            conn = init_db(db_path)
            feeds_cols = {r[1] for r in conn.execute("PRAGMA table_info(feeds)").fetchall()}
            comments_cols = {r[1] for r in conn.execute("PRAGMA table_info(comments)").fetchall()}
            media_cols = {r[1] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
            self.assertIn("guild_id", feeds_cols)
            self.assertIn("guild_id", comments_cols)
            self.assertIn("guild_id", media_cols)

            # Existing rows preserved (G11: guild_id NULL until rebuild).
            feed_row = conn.execute(
                "SELECT id, guild_id, title_text FROM feeds WHERE id=?",
                ("legacy_1",),
            ).fetchone()
            self.assertEqual(feed_row[0], "legacy_1")
            self.assertIsNone(feed_row[1])
            self.assertEqual(feed_row[2], "pre-migration row")

            comment_row = conn.execute(
                "SELECT id, guild_id FROM comments WHERE id=?", ("c1",)
            ).fetchone()
            self.assertEqual(comment_row[0], "c1")
            self.assertIsNone(comment_row[1])

            media_row = conn.execute(
                "SELECT feed_id, guild_id FROM media WHERE feed_id=?", ("legacy_1",)
            ).fetchone()
            self.assertEqual(media_row[0], "legacy_1")
            self.assertIsNone(media_row[1])

            self.assertIn("guilds", _table_names(conn))
            conn.close()

            conn2 = init_db(db_path)
            try:
                feeds_cols_2 = {r[1] for r in conn2.execute("PRAGMA table_info(feeds)").fetchall()}
                self.assertIn("guild_id", feeds_cols_2)
                preserved = conn2.execute(
                    "SELECT COUNT(*) FROM feeds WHERE id=?", ("legacy_1",)
                ).fetchone()[0]
                self.assertEqual(preserved, 1)
            finally:
                conn2.close()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_feeds_fts_unchanged(self):
        """Per N1: feeds_fts must NOT carry a guild_id column."""
        if not fts5_available(sqlite3.connect(":memory:")):
            self.skipTest("SQLite build has no FTS5")
        conn = init_db(":memory:")
        try:
            fts_cols = {r[1] for r in conn.execute("PRAGMA table_info(feeds_fts)").fetchall()}
            self.assertNotIn("guild_id", fts_cols)
            self.assertIn("feed_id", fts_cols)
            self.assertIn("title_text", fts_cols)
            self.assertIn("raw_json", fts_cols)
        finally:
            conn.close()


class TestCommentMediaSchema(unittest.TestCase):
    def test_comment_media_table_columns(self):
        conn = init_db(":memory:")
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(comment_media)"
            ).fetchall()}
            expected = {
                "comment_id", "file", "url", "type",
                "width", "height", "size", "guild_id",
            }
            self.assertEqual(cols, expected)
        finally:
            conn.close()

    def test_comment_media_index_exists(self):
        conn = init_db(":memory:")
        try:
            indexes = _index_names(conn)
            self.assertIn("idx_comment_media_comment_id", indexes)
        finally:
            conn.close()

    def test_comment_media_table_in_table_list(self):
        conn = init_db(":memory:")
        try:
            self.assertIn("comment_media", _table_names(conn))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
