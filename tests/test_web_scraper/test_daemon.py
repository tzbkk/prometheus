"""Tests for src/web_scraper/daemon.py — Daemon periodic rescan loop.

All scrapers are mocked; no network I/O. Run with:
    python3 -m pytest tests/test_web_scraper/test_daemon.py -v
"""

import os
import sys
import threading
import time
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.daemon import Daemon  # noqa: E402


def _make_feed(feed_id, comment_count=5):
    return {
        "id": feed_id,
        "createTime": 123,
        "commentCount": comment_count,
    }


def _make_mocks(feed_page=None, channels=None):
    """Create mocked scrapers + store."""
    feeds_scraper = MagicMock()
    feeds_scraper.client = MagicMock()
    feeds_scraper._accepts = MagicMock(return_value=True)

    if channels is None:
        channels = [{"channel_id": "635032487", "name": "帖子广场"}]
    feeds_scraper.client.get_guild_channels.return_value = channels

    feeds_scraper.client.get_channel_feeds.return_value = (
        feed_page if feed_page is not None else [_make_feed("B_1")],
        "",
        False,
    )

    store = MagicMock()
    store._feed_ids = {"B_1"}
    store._comment_keys = {"key1"}
    store.append_feed.return_value = True
    store.get_comment_count_last_fetched.return_value = -1
    store.get_all_feed_ids_with_comments.return_value = []
    store.is_feed_captured.return_value = True
    store._comment_keys = {"key1"}
    store.append_feed.return_value = True

    comments_scraper = MagicMock()
    comments_scraper.scrape_all.return_value = 3

    media_downloader = MagicMock()
    media_downloader.download_feed_media.return_value = 2

    return feeds_scraper, comments_scraper, media_downloader, store


def _make_daemon(feed_page=None, channels=None):
    fs, cs, md, store = _make_mocks(feed_page, channels)
    stats: dict = {}
    daemon = Daemon(fs, cs, md, store, interval_sec=0.01, stats=stats)
    return daemon, fs, cs, md, store, stats


def test_run_once_calls_feeds_scraper():
    daemon, fs, _cs, _md, _store, _stats = _make_daemon()
    daemon.run_once()
    fs.client.get_guild_channels.assert_called_once()
    fs.client.get_channel_feeds.assert_called_once_with("635032487", 7, "")


def test_run_once_calls_media_downloader():
    feed = _make_feed("B_a")
    daemon, _fs, _cs, md, _store, _stats = _make_daemon(feed_page=[feed])
    daemon.run_once()
    md.download_feed_media.assert_called_once_with(feed)


def test_run_once_calls_comments_scraper():
    feed = _make_feed("B_a")
    daemon, _fs, cs, _md, _store, _stats = _make_daemon(feed_page=[feed])
    daemon.run_once()
    cs.scrape_all.assert_any_call([feed])


def test_run_once_updates_stats():
    daemon, _fs, _cs, _md, _store, stats = _make_daemon()
    before = time.time()
    daemon.run_once()
    assert stats["last_scan_ts"] >= int(before)
    assert stats["scanned_feeds"] == 1
    daemon.run_once()
    assert stats["scanned_feeds"] == 2


def test_run_once_sets_daemon_running_during_cycle():
    daemon, fs, _cs, _md, _store, stats = _make_daemon()
    seen_during = {}

    def spy_get_channel_feeds(*args, **kwargs):
        seen_during["daemon_running"] = stats.get("daemon_running")
        return fs.client.get_channel_feeds.return_value

    fs.client.get_channel_feeds.side_effect = spy_get_channel_feeds
    daemon.run_once()
    assert seen_during["daemon_running"] is True
    assert stats["daemon_running"] is False


def test_run_once_exception_doesnt_crash_run_forever():
    daemon, fs, _cs, _md, _store, _stats = _make_daemon()
    calls = {"count": 0}

    def boom(*a, **kw):
        calls["count"] += 1
        raise RuntimeError("simulated scrape failure")

    fs.client.get_channel_feeds.side_effect = boom

    def stop_after_delay():
        time.sleep(0.05)
        daemon.stop()

    stopper = threading.Thread(target=stop_after_delay)
    stopper.start()
    daemon.run_forever()
    stopper.join()
    assert calls["count"] >= 2


def test_stop_event_stops_run_forever():
    daemon, _fs, _cs, _md, _store, _stats = _make_daemon()
    daemon._stop_event.set()
    t = threading.Thread(target=daemon.run_forever)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "run_forever did not exit after stop_event was set"


def test_signal_handler_sets_stop_event():
    daemon, _fs, _cs, _md, _store, _stats = _make_daemon()
    assert not daemon._stop_event.is_set()
    daemon._on_signal(15, None)
    assert daemon._stop_event.is_set()


def test_run_once_counts_new_feeds():
    feeds = [_make_feed(f"B_{i}") for i in range(4)]
    daemon, fs, _cs, _md, store, stats = _make_daemon(feed_page=feeds)
    store._feed_ids = set()
    store.append_feed.return_value = True
    daemon.run_once()
    assert store.append_feed.call_count == 4
    assert fs.client.get_channel_feeds.call_count == 1
    assert stats["scanned_feeds"] == 1


def test_run_once_paginates_until_all_captured():
    page1 = [_make_feed("B_new1"), _make_feed("B_new2")]
    page2 = [_make_feed("B_old1")]
    daemon, fs, _cs, _md, store, _stats = _make_daemon()
    store.append_feed.side_effect = [True, True, False]
    fs.client.get_channel_feeds.side_effect = [
        (page1, "cursor1", False),
        (page2, "cursor2", False),
    ]
    daemon.run_once()
    assert fs.client.get_channel_feeds.call_count == 2
    assert store.append_feed.call_count == 3


def test_run_once_paginates_stops_on_empty_attch():
    page1 = [_make_feed("B_a")]
    daemon, fs, _cs, _md, store, _stats = _make_daemon(feed_page=page1)
    store.append_feed.return_value = True
    daemon.run_once()
    assert fs.client.get_channel_feeds.call_count == 1


def test_write_state_creates_state_json(tmp_path):
    import json as _json
    from pathlib import Path

    feeds_scraper, comments_scraper, media_downloader, store = _make_mocks()
    store.data_dir = Path(tmp_path)
    (tmp_path / "feeds.jsonl").write_text('{"id":"B_1"}\n', encoding="utf-8")

    daemon = Daemon(feeds_scraper, comments_scraper, media_downloader, store, 1, {})
    daemon._write_state()

    state = _json.loads((tmp_path / "state.json").read_text())
    assert state["feeds"] == 1
    assert state["comments"] == 1
    assert len(state["hash"]) == 64
    assert state["hashFiles"] == ["feeds.jsonl"]
    assert state["bottomReached"] is True


def test_recheck_batches_old_feeds_round_robin():
    """Old-feed comment re-check processes only batch_size feeds per cycle."""
    old_ids = [f"B_old_{i}" for i in range(200)]
    daemon, _fs, cs, _md, store, _stats = _make_daemon()
    store.get_all_feed_ids_with_comments.return_value = old_ids

    daemon.run_once()
    args, kwargs = cs.scrape_all.call_args
    batch = args[0] if args else kwargs.get("feeds", [])
    assert len(batch) == daemon._recheck_batch_size
    assert kwargs.get("max_workers") == daemon._recheck_workers


def test_recheck_cursor_advances_each_cycle():
    """Round-robin cursor moves forward so different feeds are checked each cycle."""
    old_ids = [f"B_old_{i}" for i in range(120)]
    daemon, _fs, cs, _md, store, _stats = _make_daemon()
    store.get_all_feed_ids_with_comments.return_value = old_ids

    daemon.run_once()
    batch1 = cs.scrape_all.call_args[0][0]
    ids1 = {f["id"] for f in batch1}

    daemon.run_once()
    batch2 = cs.scrape_all.call_args[0][0]
    ids2 = {f["id"] for f in batch2}

    assert daemon._recheck_cursor == 100
    assert ids1 != ids2


def test_recheck_wraps_around():
    """Cursor wraps modulo len(old_ids) after a full rotation."""
    old_ids = [f"B_{i}" for i in range(10)]
    daemon, _fs, _cs, _md, store, _stats = _make_daemon()
    store.get_all_feed_ids_with_comments.return_value = old_ids
    daemon._recheck_batch_size = 4

    daemon.run_once()
    assert daemon._recheck_cursor == 4
    daemon.run_once()
    assert daemon._recheck_cursor == 8
    daemon.run_once()
    assert daemon._recheck_cursor == 2  # wraps: (8+4) % 10
