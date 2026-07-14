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
            "id", "create_time", "title_text", "author_nick", "author_id",
            "author_avatar", "like_count", "comment_count", "image_count",
            "video_count", "raw_json", "indexed_at",
        }
        self.assertEqual(cols, expected)

    def test_media_has_expected_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(media)").fetchall()}
        self.assertEqual(cols, {"feed_id", "file", "url", "type", "size"})

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


if __name__ == "__main__":
    unittest.main()
