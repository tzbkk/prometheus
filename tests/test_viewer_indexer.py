"""Tests for src/viewer/backend/indexer.py — JSONL → SQLite indexing.

Run with:
    python3 -m pytest tests/test_viewer_indexer.py -v
or:
    python3 -m unittest tests.test_viewer_indexer -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.viewer.backend.indexer import Indexer, discover_guilds, extract_author, extract_feed_text  # noqa: E402


def _make_feed(
    feed_id="B_test1",
    texts=None,
    nick="tester",
    poster_id="123",
    avatar="http://avatar/x.png",
    create_time="1782919000",
    like_count=5,
    comment_count=2,
    n_images=1,
    n_videos=0,
):
    if texts is None:
        texts = ["hello"]
    feed = {
        "id": feed_id,
        "createTime": create_time,
        "poster": {
            "nick": nick,
            "id": poster_id,
            "icon": {"iconUrl": avatar},
        },
        "title": {
            "contents": [
                {"text_content": {"text": t}} for t in texts
            ],
        },
        "contents": {"contents": []},
        "total_like": {"like_count": like_count},
        "commentCount": comment_count,
        "images": [{"picUrl": f"http://img/{i}.jpg"} for i in range(n_images)],
        "videos": [{"videoUrl": f"http://vid/{i}.mp4"} for i in range(n_videos)],
    }
    return feed


def _write_jsonl(path, items):
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            if isinstance(item, str):
                f.write(item)
            else:
                f.write(json.dumps(item, ensure_ascii=False))
            f.write("\n")


class TestExtractFeedText(unittest.TestCase):
    def test_single_text_segment(self):
        feed = _make_feed(texts=["hello world"])
        self.assertEqual(extract_feed_text(feed), "hello world")

    def test_multiple_text_segments_joined_with_space(self):
        feed = _make_feed(texts=["foo", "bar", "baz"])
        self.assertEqual(extract_feed_text(feed), "foo bar baz")

    def test_empty_contents(self):
        feed = _make_feed(texts=[])
        self.assertEqual(extract_feed_text(feed), "")

    def test_no_title_key(self):
        self.assertEqual(extract_feed_text({}), "")


class TestExtractAuthor(unittest.TestCase):
    def test_full_poster(self):
        feed = _make_feed()
        author = extract_author(feed)
        self.assertEqual(author["nick"], "tester")
        self.assertEqual(author["id"], "123")
        self.assertEqual(author["avatar"], "http://avatar/x.png")

    def test_missing_poster(self):
        author = extract_author({})
        self.assertIsNone(author["nick"])
        self.assertIsNone(author["id"])
        self.assertIsNone(author["avatar"])

    def test_missing_icon(self):
        feed = {"poster": {"nick": "x", "id": "9"}}
        author = extract_author(feed)
        self.assertEqual(author["nick"], "x")
        self.assertIsNone(author["avatar"])


class TestBuildAll(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.feeds_path = os.path.join(self.tmpdir, "feeds.jsonl")
        self.media_path = os.path.join(self.tmpdir, "media_index.jsonl")

        feeds = [
            _make_feed("B_a", texts=["alpha manga"], n_images=2),
            _make_feed("B_b", texts=["beta"], n_images=1, n_videos=1),
            _make_feed("B_c", texts=["gamma"]),
        ]
        _write_jsonl(self.feeds_path, feeds)

        media = [
            {"url": "http://img/a1.jpg", "file": "a1.jpg", "type": "image",
             "size": 100, "source": "B_a"},
            {"url": "http://img/a2.jpg", "file": "a2.jpg", "type": "image",
             "size": 200, "source": "B_a"},
            {"url": "http://img/b1.jpg", "file": "b1.jpg", "type": "image",
             "size": 300, "source": "B_b"},
            {"url": "http://vid/b1.mp4", "file": "b1.mp4", "type": "video",
             "size": 999, "source": "B_b"},
        ]
        _write_jsonl(self.media_path, media)

        self.indexer = Indexer(self.db_path)
        list(self.indexer.build_all(self.feeds_path, self.media_path))

    def tearDown(self):
        self._tmp.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def test_feed_count(self):
        conn = self._connect()
        count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        conn.close()
        self.assertEqual(count, 3)

    def test_fts_count(self):
        conn = self._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM feeds_fts").fetchone()[0]
        except sqlite3.OperationalError:
            self.skipTest("FTS5 not available")
        conn.close()
        self.assertEqual(count, 3)

    def test_media_count(self):
        conn = self._connect()
        count = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        conn.close()
        self.assertEqual(count, 4)

    def test_feed_fields_populated(self):
        conn = self._connect()
        row = conn.execute(
            "SELECT id, create_time, title_text, author_nick, author_id, "
            "author_avatar, like_count, comment_count, image_count, video_count "
            "FROM feeds WHERE id = 'B_b'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "B_b")
        self.assertEqual(row[1], 1782919000)
        self.assertEqual(row[2], "beta")
        self.assertEqual(row[3], "tester")
        self.assertEqual(row[4], "123")
        self.assertEqual(row[5], "http://avatar/x.png")
        self.assertEqual(row[6], 5)
        self.assertEqual(row[7], 2)
        self.assertEqual(row[8], 1)
        self.assertEqual(row[9], 1)

    def test_fts_search_ascii(self):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT feed_id FROM feeds_fts WHERE feeds_fts MATCH 'manga'"
            ).fetchall()
        except sqlite3.OperationalError:
            self.skipTest("FTS5 not available")
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "B_a")

    def test_last_offset_stored(self):
        conn = self._connect()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'last_offset'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertGreater(int(row[0]), 0)

    def test_raw_json_stored(self):
        conn = self._connect()
        row = conn.execute(
            "SELECT raw_json FROM feeds WHERE id = 'B_a'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        data = json.loads(row[0])
        self.assertEqual(data["id"], "B_a")

    def test_progress_yields_increasing_values(self):
        indexer = Indexer(os.path.join(self.tmpdir, "progress.db"))
        progresses = list(indexer.build_all(self.feeds_path, self.media_path))
        self.assertGreater(len(progresses), 0)
        self.assertEqual(progresses[-1], 100.0)


class TestMalformedLineTolerance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.feeds_path = os.path.join(self.tmpdir, "feeds.jsonl")
        self.media_path = os.path.join(self.tmpdir, "media_index.jsonl")

        items = [
            _make_feed("B_good1", texts=["first"]),
            "{this is not valid json}",
            _make_feed("B_good2", texts=["second"]),
            "",
            _make_feed("B_good3", texts=["third"]),
        ]
        _write_jsonl(self.feeds_path, items)
        _write_jsonl(self.media_path, [])

    def tearDown(self):
        self._tmp.cleanup()

    def test_malformed_lines_skipped(self):
        indexer = Indexer(self.db_path)
        list(indexer.build_all(self.feeds_path, self.media_path))
        conn = sqlite3.connect(self.db_path)
        ids = [r[0] for r in conn.execute("SELECT id FROM feeds ORDER BY id").fetchall()]
        conn.close()
        self.assertEqual(ids, ["B_good1", "B_good2", "B_good3"])


class TestBuildIncremental(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.feeds_path = os.path.join(self.tmpdir, "feeds.jsonl")
        self.media_path = os.path.join(self.tmpdir, "media_index.jsonl")

        _write_jsonl(
            self.feeds_path,
            [_make_feed("B_old1", texts=["old"]), _make_feed("B_old2", texts=["older"])],
        )
        _write_jsonl(self.media_path, [])

        self.indexer = Indexer(self.db_path)
        list(self.indexer.build_all(self.feeds_path, self.media_path))

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_new_lines(self):
        list(self.indexer.build_incremental(self.feeds_path, self.media_path))
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_appended_lines_indexed(self):
        with open(self.feeds_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_make_feed("B_new1", texts=["fresh"]), ensure_ascii=False))
            f.write("\n")
            f.write(json.dumps(_make_feed("B_new2", texts=["fresher"]), ensure_ascii=False))
            f.write("\n")

        list(self.indexer.build_incremental(self.feeds_path, self.media_path))

        conn = sqlite3.connect(self.db_path)
        ids = [r[0] for r in conn.execute("SELECT id FROM feeds ORDER BY id").fetchall()]
        conn.close()
        self.assertEqual(ids, ["B_new1", "B_new2", "B_old1", "B_old2"])

    def test_double_incremental_idempotent(self):
        with open(self.feeds_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_make_feed("B_new1", texts=["x"]), ensure_ascii=False))
            f.write("\n")

        list(self.indexer.build_incremental(self.feeds_path, self.media_path))
        list(self.indexer.build_incremental(self.feeds_path, self.media_path))

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        conn.close()
        self.assertEqual(count, 3)


class TestMediaDedup(unittest.TestCase):
    """The indexer must deduplicate media_index.jsonl entries by URL.

    Legacy inject.js wrote extensionless filenames (``abc123``); web_scraper
    writes ``abc123.jpg``.  Both append to the same file, producing duplicate
    entries for the same URL.  The indexer must keep only one — preferring
    the extensioned version so the file exists on disk.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.feeds_path = os.path.join(self.tmpdir, "feeds.jsonl")
        self.media_path = os.path.join(self.tmpdir, "media_index.jsonl")

        _write_jsonl(self.feeds_path, [_make_feed("B_d", texts=["dedup"])])
        _write_jsonl(self.media_path, [
            {"url": "http://img/d.jpg", "file": "dddd.jpg", "type": "image",
             "size": 100, "source": "B_d"},
            {"url": "http://img/d.jpg", "file": "dddd", "type": "image",
             "size": 100, "source": "B_d"},
            {"url": "http://img/e.jpg", "file": "eeee", "type": "image",
             "size": 200, "source": "B_d"},
            {"url": "http://img/e.jpg", "file": "eeee", "type": "image",
             "size": 200, "source": "B_d"},
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_duplicate_urls_collapsed(self):
        indexer = Indexer(self.db_path)
        list(indexer.build_all(self.feeds_path, self.media_path))
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT url, file FROM media WHERE feed_id = 'B_d' ORDER BY url"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)

    def test_extension_preferred(self):
        indexer = Indexer(self.db_path)
        list(indexer.build_all(self.feeds_path, self.media_path))
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT file FROM media WHERE feed_id = 'B_d' AND url = 'http://img/d.jpg'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "dddd.jpg")


class TestDiscoverGuilds(unittest.TestCase):
    """discover_guilds: scan data_dir/*/feeds.jsonl, numeric guild_ids only (G16)."""

    def test_discover_guilds_finds_numeric_dirs(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            (root / "111").mkdir()
            (root / "111" / "feeds.jsonl").write_text("{}", encoding="utf-8")
            (root / "222").mkdir()
            (root / "222" / "feeds.jsonl").write_text("{}", encoding="utf-8")
            # non-numeric dir — must be skipped
            (root / "media").mkdir()
            (root / "media" / "feeds.jsonl").write_text("{}", encoding="utf-8")
            # numeric dir WITHOUT feeds.jsonl — must be skipped
            (root / "333").mkdir()
            # non-dir entry — must be skipped
            (root / "444").write_text("not a dir", encoding="utf-8")

            found = discover_guilds(str(root))
            gids = [g[0] for g in found]
            self.assertEqual(gids, ["111", "222"])
            for gid, p in found:
                self.assertIsInstance(p, Path)
                self.assertEqual(p.name, gid)
        finally:
            tmp.cleanup()

    def test_discover_guilds_missing_dir_returns_empty(self):
        self.assertEqual(discover_guilds("/nonexistent/path/xyz"), [])


class TestBuildAllGuilds(unittest.TestCase):
    """build_all_guilds: discover + index each guild with correct guild_id tag."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.data_dir = os.path.join(self.tmpdir, "data")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.makedirs(self.data_dir)

        for gid, feed_ids in [("111", ["B_a1", "B_a2"]), ("222", ["B_b1"])]:
            gdir = os.path.join(self.data_dir, gid)
            os.makedirs(gdir)
            feeds = [_make_feed(fid, texts=[f"text-{fid}"]) for fid in feed_ids]
            _write_jsonl(os.path.join(gdir, "feeds.jsonl"), feeds)
            _write_jsonl(os.path.join(gdir, "media_index.jsonl"), [])

    def tearDown(self):
        self._tmp.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def test_build_all_guilds_indexes_all(self):
        indexer = Indexer(self.db_path)
        indexer._find_conf_dir = lambda: None  # no conf in tempdir → no-op enrichment
        list(indexer.build_all_guilds(self.data_dir))

        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, guild_id FROM feeds ORDER BY guild_id, id"
            ).fetchall()
            conn.close()
        finally:
            pass
        self.assertEqual(
            rows,
            [("B_a1", "111"), ("B_a2", "111"), ("B_b1", "222")],
        )

    def test_build_all_guilds_per_guild_feed_counts(self):
        indexer = Indexer(self.db_path)
        indexer._find_conf_dir = lambda: None
        list(indexer.build_all_guilds(self.data_dir))

        conn = self._connect()
        rows = conn.execute(
            "SELECT guild_id, feeds FROM guilds ORDER BY guild_id"
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [("111", 2), ("222", 1)])

    def test_build_all_guilds_per_guild_offset_keys(self):
        indexer = Indexer(self.db_path)
        indexer._find_conf_dir = lambda: None
        list(indexer.build_all_guilds(self.data_dir))

        conn = self._connect()
        keys = [r[0] for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'offset:%' ORDER BY key"
        ).fetchall()]
        conn.close()
        self.assertEqual(keys, ["offset:111", "offset:222"])

    def test_build_all_guilds_media_tagged_with_guild_id(self):
        media_path = os.path.join(self.data_dir, "111", "media_index.jsonl")
        _write_jsonl(media_path, [
            {"url": "http://img/a1.jpg", "file": "a1.jpg", "type": "image",
             "size": 100, "source": "B_a1"},
        ])
        indexer = Indexer(self.db_path)
        indexer._find_conf_dir = lambda: None
        list(indexer.build_all_guilds(self.data_dir))

        conn = self._connect()
        row = conn.execute(
            "SELECT feed_id, guild_id FROM media WHERE file = 'a1.jpg'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "B_a1")
        self.assertEqual(row[1], "111")


class TestPerGuildOffsetGuard(unittest.TestCase):
    """B2: new guild B is indexed even when guild A already has rows."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.data_dir = os.path.join(self.tmpdir, "data")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.makedirs(self.data_dir)
        gdir_a = os.path.join(self.data_dir, "111")
        os.makedirs(gdir_a)
        _write_jsonl(
            os.path.join(gdir_a, "feeds.jsonl"),
            [_make_feed("B_a1", texts=["alpha"])],
        )
        _write_jsonl(os.path.join(gdir_a, "media_index.jsonl"), [])

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_guild_indexes_when_other_guild_already_present(self):
        indexer = Indexer(self.db_path)
        indexer._find_conf_dir = lambda: None
        # First pass: only guild A present, indexed fully.
        list(indexer.build_incremental_guilds(self.data_dir))
        conn = sqlite3.connect(self.db_path)
        count_a = conn.execute(
            "SELECT COUNT(*) FROM feeds WHERE guild_id = '111'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count_a, 1)

        # Now guild B appears (new dir).
        gdir_b = os.path.join(self.data_dir, "222")
        os.makedirs(gdir_b)
        _write_jsonl(
            os.path.join(gdir_b, "feeds.jsonl"),
            [_make_feed("B_b1", texts=["beta"])],
        )
        _write_jsonl(os.path.join(gdir_b, "media_index.jsonl"), [])

        # Second incremental pass: B MUST be indexed (B2 — old global guard
        # would have skipped this because A's rows already exist).
        list(indexer.build_incremental_guilds(self.data_dir))
        conn = sqlite3.connect(self.db_path)
        count_b = conn.execute(
            "SELECT COUNT(*) FROM feeds WHERE guild_id = '222'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count_b, 1)

    def test_incremental_idempotent_per_guild(self):
        indexer = Indexer(self.db_path)
        indexer._find_conf_dir = lambda: None
        list(indexer.build_incremental_guilds(self.data_dir))
        list(indexer.build_incremental_guilds(self.data_dir))
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


class TestGuildNamesEnrichedFromConf(unittest.TestCase):
    """G28: conf/guilds.conf.json names are upserted into SQLite guilds table."""

    def test_guild_names_enriched_from_conf(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            tmpdir = tmp.name
            data_dir = os.path.join(tmpdir, "data")
            guild_id = "7743321643036658"
            gdir = os.path.join(data_dir, guild_id)
            os.makedirs(gdir)
            _write_jsonl(
                os.path.join(gdir, "feeds.jsonl"),
                [_make_feed("B_a", texts=["alpha"])],
            )
            _write_jsonl(os.path.join(gdir, "media_index.jsonl"), [])

            conf_dir = os.path.join(tmpdir, "conf")
            os.makedirs(conf_dir)
            with open(os.path.join(conf_dir, "guilds.conf.json"),
                      "w", encoding="utf-8") as f:
                json.dump({
                    "guilds": [{
                        "guild_id": guild_id,
                        "guild_number": "Takagi3channel",
                        "name": "TestGuild",
                    }],
                }, f)

            db_path = os.path.join(tmpdir, "test.db")
            indexer = Indexer(db_path)
            indexer._find_conf_dir = lambda: Path(conf_dir)
            list(indexer.build_all_guilds(data_dir))

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT guild_id, guild_number, name, feeds FROM guilds "
                "WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], guild_id)
            self.assertEqual(row[1], "Takagi3channel")
            self.assertEqual(row[2], "TestGuild")
            self.assertEqual(row[3], 1)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()