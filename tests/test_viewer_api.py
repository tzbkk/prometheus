"""Unit tests for viewer API endpoints (api.py) and media routing (server.py).

Builds a fresh SQLite DB via schema.init_db, inserts fixture rows, then calls
the handler functions directly for the API contract tests. For G7 media
routing, an in-process ViewerServer is started on port 0 and exercised via
real HTTP so the full path-traversal guard chain is covered.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_viewer_api.py -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from typing import Any, Dict, List, cast

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.viewer.backend.api import (  # noqa: E402
    handle_feed_comments,
    handle_feed_detail,
    handle_feeds,
    handle_guilds,
    handle_rebuild,
    handle_search,
    handle_stats,
)
from src.viewer.backend.schema import init_db  # noqa: E402
from src.viewer.backend.server import ViewerServer  # noqa: E402


def _body_as_list(result) -> List[Dict[str, Any]]:
    status, body = result
    assert isinstance(body, list), f"expected list body, got {type(body)}"
    return cast(List[Dict[str, Any]], body)


def _body_as_dict(result) -> Dict[str, Any]:
    status, body = result
    assert isinstance(body, dict), f"expected dict body, got {type(body)}"
    return cast(Dict[str, Any], body)


def _insert_feed(conn, feed_id, guild_id, title="hello", create_time=1000,
                 nick="tester"):
    conn.execute(
        "INSERT INTO feeds (id, guild_id, create_time, title_text, author_nick, "
        "author_id, author_avatar, like_count, comment_count, image_count, "
        "video_count, raw_json, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (feed_id, guild_id, create_time, title, nick, "uid", "avatar", 1, 0,
         0, 0, json.dumps({"title": title}), "2024-01-01T00:00:00Z"),
    )


def _insert_guild(conn, guild_id, guild_number, name, feeds):
    conn.execute(
        "INSERT INTO guilds (guild_id, guild_number, name, feeds, indexed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, guild_number, name, feeds, "2024-01-01T00:00:00Z"),
    )


def _insert_comment(conn, comment_id, feed_id, guild_id, text="hello",
                    sequence=1, parent_id=None):
    conn.execute(
        "INSERT INTO comments (id, feed_id, guild_id, parent_id, create_time, "
        "author_nick, author_avatar, content_text, ip_location, like_count, "
        "reply_count, sequence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (comment_id, feed_id, guild_id, parent_id, 1000, "nick", "avatar",
         text, "loc", 0, 0, sequence),
    )


def _insert_comment_media(conn, comment_id, file_, url, type_="image",
                          width=800, height=600, size=100, guild_id="111"):
    conn.execute(
        "INSERT INTO comment_media (comment_id, file, url, type, width, height, "
        "size, guild_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (comment_id, file_, url, type_, width, height, size, guild_id),
    )


def _build_db(db_path, *, feeds=None, guilds=None):
    conn = init_db(db_path)
    if guilds:
        for g in guilds:
            _insert_guild(conn, **g)
    if feeds:
        for f in feeds:
            _insert_feed(conn, **f)
    conn.commit()
    conn.close()


class TestHandleGuilds(unittest.TestCase):

    def test_returns_guilds_ordered_by_feeds_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            _build_db(db_path, guilds=[
                {"guild_id": "111", "guild_number": "slug1", "name": "G1",
                 "feeds": 5},
                {"guild_id": "222", "guild_number": "slug2", "name": "G2",
                 "feeds": 10},
                {"guild_id": "333", "guild_number": "slug3", "name": "G3",
                 "feeds": 1},
            ])
            status, body = handle_guilds(db_path)
            self.assertEqual(status, 200)
            guilds = _body_as_list((status, body))
            self.assertEqual([g["guild_id"] for g in guilds], ["222", "111", "333"])
            for g in guilds:
                self.assertEqual(
                    set(g.keys()),
                    {"guild_id", "guild_number", "name", "feeds"},
                )

    def test_returns_empty_list_when_no_guilds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            _build_db(db_path)
            status, body = handle_guilds(db_path)
            self.assertEqual(status, 200)
            self.assertEqual(_body_as_list((status, body)), [])


class TestHandleFeedsGuildFilter(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        _build_db(self.db_path, feeds=[
            {"feed_id": "B_a1", "guild_id": "111", "title": "alpha",
             "create_time": 1000},
            {"feed_id": "B_a2", "guild_id": "111", "title": "beta",
             "create_time": 2000},
            {"feed_id": "B_b1", "guild_id": "222", "title": "gamma",
             "create_time": 3000},
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_filter_returns_only_matching_guild(self):
        status, body = handle_feeds(self.db_path, {"guild": ["111"]})
        self.assertEqual(status, 200)
        feeds = _body_as_list((status, body))
        ids = {f["id"] for f in feeds}
        self.assertEqual(ids, {"B_a1", "B_a2"})

    def test_filter_with_no_matches_returns_empty(self):
        status, body = handle_feeds(self.db_path, {"guild": ["999"]})
        self.assertEqual(status, 200)
        self.assertEqual(_body_as_list((status, body)), [])

    def test_no_guild_param_returns_all_guilds(self):
        status, body = handle_feeds(self.db_path, {})
        self.assertEqual(status, 200)
        self.assertEqual(len(_body_as_list((status, body))), 3)

    def test_feeds_include_guild_id_field(self):
        status, body = handle_feeds(self.db_path, {})
        self.assertEqual(status, 200)
        feeds = _body_as_list((status, body))
        for f in feeds:
            self.assertIn("guild_id", f)
            self.assertIsNotNone(f["guild_id"])


class TestHandleSearchGuildFilter(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        _build_db(self.db_path, feeds=[
            {"feed_id": "B_a1", "guild_id": "111", "title": "test alpha"},
            {"feed_id": "B_a2", "guild_id": "111", "title": "other"},
            {"feed_id": "B_b1", "guild_id": "222", "title": "test beta"},
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_search_with_guild_filter(self):
        status, body = handle_search(
            self.db_path, {"q": ["test"], "guild": ["111"]},
        )
        self.assertEqual(status, 200)
        feeds = _body_as_list((status, body))
        ids = {f["id"] for f in feeds}
        self.assertEqual(ids, {"B_a1"})

    def test_search_without_guild_finds_across_all(self):
        status, body = handle_search(self.db_path, {"q": ["test"]})
        self.assertEqual(status, 200)
        feeds = _body_as_list((status, body))
        ids = {f["id"] for f in feeds}
        self.assertEqual(ids, {"B_a1", "B_b1"})

    def test_search_missing_q_returns_400(self):
        status, body = handle_search(self.db_path, {})
        self.assertEqual(status, 400)
        self.assertIn("error", _body_as_dict((status, body)))


class TestHandleFeedDetailGuildId(unittest.TestCase):

    def test_feed_detail_includes_guild_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            _build_db(db_path, feeds=[
                {"feed_id": "B_x", "guild_id": "111", "title": "hello"},
            ])
            status, body = handle_feed_detail(db_path, "B_x")
            self.assertEqual(status, 200)
            feed = _body_as_dict((status, body))
            self.assertEqual(feed["id"], "B_x")
            self.assertEqual(feed["guild_id"], "111")

    def test_feed_detail_404_for_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            _build_db(db_path)
            status, body = handle_feed_detail(db_path, "B_nope")
            self.assertEqual(status, 404)


class TestHandleStats(unittest.TestCase):

    def test_stats_returns_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            _build_db(db_path, feeds=[
                {"feed_id": "B_a", "guild_id": "111"},
            ])
            status, body = handle_stats(db_path)
            self.assertEqual(status, 200)
            stats = _body_as_dict((status, body))
            for key in ("feed_count", "media_count", "db_size", "last_indexed",
                        "total_feeds", "total_media", "last_indexed_at"):
                self.assertIn(key, stats)
            self.assertEqual(stats["feed_count"], 1)


class TestHandleRebuildLock(unittest.TestCase):

    def test_rebuild_accepts_lock_kwarg(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            data_dir = os.path.join(tmp, "data")
            os.makedirs(data_dir)
            _build_db(db_path)
            lock = threading.Lock()
            status, body = handle_rebuild(db_path, data_dir, rebuild_lock=lock)
            self.assertEqual(status, 200)
            self.assertTrue(_body_as_dict((status, body))["ok"])


class TestMediaRouting(unittest.TestCase):
    """G7: /media/<guild_id>/<file> 2-segment routing with traversal guard.

    The path-traversal guard has multiple layers (numeric guild_id, no path
    separators in filename, resolve-then-verify). Tests enumerate each layer
    to catch regressions if any is silently removed.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls._tmp.name) / "data"
        cls.static_dir = Path(cls._tmp.name) / "static"
        cls.db_path = Path(cls._tmp.name) / "test.db"

        guild_media = cls.data_dir / "111" / "media"
        guild_media.mkdir(parents=True)
        cls.media_filename = "photo.jpg"
        cls.media_bytes = b"\xff\xd8\xff\xe0fake-jpeg-data"
        (guild_media / cls.media_filename).write_bytes(cls.media_bytes)

        guild2_media = cls.data_dir / "222" / "media"
        guild2_media.mkdir(parents=True)
        (guild2_media / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        cls.static_dir.mkdir(parents=True)
        init_db(str(cls.db_path))

        cls.server = ViewerServer(
            host="127.0.0.1",
            port=0,
            static_dir=str(cls.static_dir),
            data_dir=str(cls.data_dir),
            db_path=str(cls.db_path),
        )
        cls._server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls._server_thread.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls._tmp.cleanup()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.server.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_serves_file_with_2_segment_path(self):
        status, body = self._get(f"/media/111/{self.media_filename}")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.media_bytes)

    def test_serves_per_guild_isolation(self):
        status, body = self._get("/media/222/secret.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x89PNG\r\n\x1a\n")

        status, _ = self._get("/media/111/secret.png")
        self.assertEqual(status, 404)

    def test_rejects_non_numeric_guild_id(self):
        status, _ = self._get("/media/notnumeric/file.jpg")
        self.assertEqual(status, 404)

    def test_rejects_missing_filename(self):
        status, _ = self._get("/media/111/")
        self.assertEqual(status, 404)

    def test_rejects_single_segment_path(self):
        status, _ = self._get("/media/foo")
        self.assertEqual(status, 404)

    def test_rejects_three_segment_path(self):
        status, _ = self._get("/media/111/foo/bar")
        self.assertIn(status, (403, 404))

    def test_rejects_parent_dir_traversal(self):
        # URL-encoded ../ so the client does not pre-normalize. After unquote
        # the second segment becomes "../.." which the filename guards reject.
        status, _ = self._get("/media/111/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
        self.assertIn(status, (403, 404))

    def test_rejects_nonexistent_file(self):
        status, _ = self._get("/media/111/doesnotexist.jpg")
        self.assertEqual(status, 404)


class TestHandleFeedCommentsMedia(unittest.TestCase):
    """handle_feed_comments returns a `media` array per comment."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        _build_db(self.db_path, feeds=[
            {"feed_id": "B_f1", "guild_id": "111", "title": "feed"},
        ])
        conn = init_db(self.db_path)
        _insert_comment(conn, "C_a", "B_f1", "111", text="with media",
                        sequence=1)
        _insert_comment(conn, "C_b", "B_f1", "111", text="no media",
                        sequence=2)
        _insert_comment_media(
            conn, "C_a", "a.jpg", "http://x/a.jpg",
            type_="image", width=800, height=600, size=12345, guild_id="111",
        )
        _insert_comment_media(
            conn, "C_a", "a2.jpg", "http://x/a2.jpg",
            type_="image", width=400, height=300, size=6789, guild_id="111",
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_comments_carry_media_array(self):
        status, body = handle_feed_comments(self.db_path, "B_f1")
        self.assertEqual(status, 200)
        comments = _body_as_list((status, body))
        by_id = {c["id"]: c for c in comments}
        self.assertEqual(by_id["C_a"]["media"], [
            {"file": "a.jpg", "url": "http://x/a.jpg", "type": "image",
             "width": 800, "height": 600},
            {"file": "a2.jpg", "url": "http://x/a2.jpg", "type": "image",
             "width": 400, "height": 300},
        ])

    def test_comment_without_media_returns_empty_array(self):
        status, body = handle_feed_comments(self.db_path, "B_f1")
        comments = _body_as_list((status, body))
        by_id = {c["id"]: c for c in comments}
        self.assertEqual(by_id["C_b"]["media"], [])

    def test_feed_with_no_comments_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            _build_db(db_path, feeds=[{"feed_id": "B_x", "guild_id": "111"}])
            status, body = handle_feed_comments(db_path, "B_x")
            self.assertEqual(status, 200)
            self.assertEqual(_body_as_list((status, body)), [])

    def test_missing_feed_id_returns_404(self):
        status, body = handle_feed_comments(self.db_path, "")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
