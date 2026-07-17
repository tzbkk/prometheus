"""Tests for src/web_scraper/daemon.py — Daemon periodic rescan loop.

Multi-guild (plan §2): Daemon now takes a list of GuildContext objects
instead of individual scrapers/store. Per-guild recheck state
(cursor/batch/workers) lives on GuildContext (G3) so each guild rotates
independently.

All scrapers are mocked; no network I/O. Run with:
    python3 -m pytest tests/test_web_scraper/test_daemon.py -v
"""

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.config import Guild  # noqa: E402
from src.web_scraper.daemon import Daemon  # noqa: E402


def _make_feed(feed_id, comment_count=5):
    return {
        "id": feed_id,
        "createTime": 123,
        "commentCount": comment_count,
    }


@dataclass
class _Ctx:
    """Minimal GuildContext stand-in for daemon tests (mirrors the real
    dataclass in __main__.py). Daemon duck-types this — only the fields
    it touches need to be present."""

    guild: Guild
    feeds_scraper: Any = field(default_factory=MagicMock)
    comments_scraper: Any = field(default_factory=MagicMock)
    media_downloader: Any = field(default_factory=MagicMock)
    store: Any = field(default_factory=MagicMock)
    client: Any = field(default_factory=MagicMock)
    _recheck_cursor: int = 0
    _recheck_batch_size: int = 50
    _recheck_workers: int = 3


def _make_guild_context(
    feed_page=None,
    channels=None,
    guild_id: str = "777",
    guild_number: str = "TestChannel",
    name: str = "test",
    old_ids=None,
):
    """Build a single mocked GuildContext (mirrors __main__.GuildContext)."""
    feeds_scraper = MagicMock()
    feeds_scraper.channel_id = guild_id
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
    store.get_all_feed_ids_with_comments.return_value = (
        old_ids if old_ids is not None else []
    )
    store.is_feed_captured.return_value = True

    comments_scraper = MagicMock()
    comments_scraper.scrape_all.return_value = 3

    media_downloader = MagicMock()
    media_downloader.download_feed_media.return_value = 2
    media_downloader._seen = set()

    return _Ctx(
        guild=Guild(guild_id=guild_id, guild_number=guild_number, name=name),
        feeds_scraper=feeds_scraper,
        comments_scraper=comments_scraper,
        media_downloader=media_downloader,
        store=store,
        client=feeds_scraper.client,
    )


def _make_daemon(feed_page=None, channels=None, old_ids=None, n_guilds: int = 1):
    """Build a Daemon backed by ``n_guilds`` mocked GuildContexts.

    Returns ``(daemon, ctxs, stats)``. For ``n_guilds == 1`` the helper
    keeps the old single-value return shape (single ctx) for tests that
    only care about one guild.
    """
    ctxs = [
        _make_guild_context(
            feed_page=feed_page,
            channels=channels,
            guild_id=str(777 + i),
            name=f"test{i}",
            old_ids=old_ids,
        )
        for i in range(n_guilds)
    ]
    stats: dict = {}
    daemon = Daemon(ctxs, interval_sec=0.01, stats=stats)
    return daemon, ctxs, stats


def test_run_once_calls_feeds_scraper():
    daemon, ctxs, _stats = _make_daemon()
    daemon.run_once()
    ctxs[0].feeds_scraper.client.get_guild_channels.assert_called_once()
    ctxs[0].feeds_scraper.client.get_channel_feeds.assert_called_once_with(
        "635032487", 7, ""
    )


def test_run_once_calls_media_downloader():
    feed = _make_feed("B_a")
    daemon, ctxs, _stats = _make_daemon(feed_page=[feed])
    daemon.run_once()
    ctxs[0].media_downloader.download_feed_media.assert_called_once_with(feed)


def test_run_once_calls_comments_scraper():
    feed = _make_feed("B_a")
    daemon, ctxs, _stats = _make_daemon(feed_page=[feed])
    daemon.run_once()
    ctxs[0].comments_scraper.scrape_all.assert_any_call([feed])


def test_run_once_updates_stats():
    daemon, _ctxs, stats = _make_daemon()
    before = time.time()
    daemon.run_once()
    assert stats["last_scan_ts"] >= int(before)
    assert stats["scanned_feeds"] == 1
    daemon.run_once()
    assert stats["scanned_feeds"] == 2


def test_run_once_sets_daemon_running_during_cycle():
    daemon, ctxs, stats = _make_daemon()
    seen_during = {}

    def spy_get_channel_feeds(*args, **kwargs):
        seen_during["daemon_running"] = stats.get("daemon_running")
        return ctxs[0].feeds_scraper.client.get_channel_feeds.return_value

    ctxs[0].feeds_scraper.client.get_channel_feeds.side_effect = spy_get_channel_feeds
    daemon.run_once()
    assert seen_during["daemon_running"] is True
    assert stats["daemon_running"] is False


def test_run_once_exception_doesnt_crash_run_forever():
    daemon, ctxs, _stats = _make_daemon()
    calls = {"count": 0}

    def boom(*a, **kw):
        calls["count"] += 1
        raise RuntimeError("simulated scrape failure")

    ctxs[0].feeds_scraper.client.get_channel_feeds.side_effect = boom

    def stop_after_delay():
        time.sleep(0.05)
        daemon.stop()

    stopper = threading.Thread(target=stop_after_delay)
    stopper.start()
    daemon.run_forever()
    stopper.join()
    assert calls["count"] >= 2


def test_stop_event_stops_run_forever():
    daemon, _ctxs, _stats = _make_daemon()
    daemon._stop_event.set()
    t = threading.Thread(target=daemon.run_forever)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "run_forever did not exit after stop_event was set"


def test_signal_handler_sets_stop_event():
    daemon, _ctxs, _stats = _make_daemon()
    assert not daemon._stop_event.is_set()
    daemon._on_signal(15, None)
    assert daemon._stop_event.is_set()


def test_run_once_counts_new_feeds():
    feeds = [_make_feed(f"B_{i}") for i in range(4)]
    daemon, ctxs, stats = _make_daemon(feed_page=feeds)
    ctxs[0].store._feed_ids = set()
    ctxs[0].store.append_feed.return_value = True
    daemon.run_once()
    assert ctxs[0].store.append_feed.call_count == 4
    assert ctxs[0].feeds_scraper.client.get_channel_feeds.call_count == 1
    assert stats["scanned_feeds"] == 1


def test_run_once_paginates_until_all_captured():
    page1 = [_make_feed("B_new1"), _make_feed("B_new2")]
    page2 = [_make_feed("B_old1")]
    daemon, ctxs, _stats = _make_daemon()
    ctxs[0].store.append_feed.side_effect = [True, True, False]
    ctxs[0].feeds_scraper.client.get_channel_feeds.side_effect = [
        (page1, "cursor1", False),
        (page2, "cursor2", False),
    ]
    daemon.run_once()
    assert ctxs[0].feeds_scraper.client.get_channel_feeds.call_count == 2
    assert ctxs[0].store.append_feed.call_count == 3


def test_run_once_paginates_stops_on_empty_attch():
    page1 = [_make_feed("B_a")]
    daemon, ctxs, _stats = _make_daemon(feed_page=page1)
    ctxs[0].store.append_feed.return_value = True
    daemon.run_once()
    assert ctxs[0].feeds_scraper.client.get_channel_feeds.call_count == 1


def test_write_state_creates_state_json(tmp_path):
    import json as _json
    from pathlib import Path

    ctx = _make_guild_context()
    ctx.store.data_dir = Path(tmp_path)
    (tmp_path / "feeds.jsonl").write_text('{"id":"B_1"}\n', encoding="utf-8")

    daemon = Daemon([ctx], 1, {})
    daemon._write_state(ctx)

    state = _json.loads((tmp_path / "state.json").read_text())
    assert state["feeds"] == 1
    assert state["comments"] == 1
    assert len(state["hash"]) == 64
    assert state["hashFiles"] == ["feeds.jsonl"]
    assert state["bottomReached"] is True


def test_recheck_batches_old_feeds_round_robin():
    """Old-feed comment re-check processes only batch_size feeds per cycle."""
    old_ids = [f"B_old_{i}" for i in range(200)]
    daemon, ctxs, stats = _make_daemon(old_ids=old_ids)
    daemon.run_once()
    args, kwargs = ctxs[0].comments_scraper.scrape_all.call_args
    batch = args[0] if args else kwargs.get("feeds", [])
    assert len(batch) == ctxs[0]._recheck_batch_size
    assert kwargs.get("max_workers") == ctxs[0]._recheck_workers


def test_recheck_cursor_advances_each_cycle():
    """Round-robin cursor moves forward so different feeds are checked each cycle."""
    old_ids = [f"B_old_{i}" for i in range(120)]
    daemon, ctxs, _stats = _make_daemon(old_ids=old_ids)

    daemon.run_once()
    batch1 = ctxs[0].comments_scraper.scrape_all.call_args[0][0]
    ids1 = {f["id"] for f in batch1}

    daemon.run_once()
    batch2 = ctxs[0].comments_scraper.scrape_all.call_args[0][0]
    ids2 = {f["id"] for f in batch2}

    assert ctxs[0]._recheck_cursor == 100
    assert ids1 != ids2


def test_recheck_wraps_around():
    """Cursor wraps modulo len(old_ids) after a full rotation."""
    old_ids = [f"B_{i}" for i in range(10)]
    daemon, ctxs, _stats = _make_daemon(old_ids=old_ids)
    ctxs[0]._recheck_batch_size = 4

    daemon.run_once()
    assert ctxs[0]._recheck_cursor == 4
    daemon.run_once()
    assert ctxs[0]._recheck_cursor == 8
    daemon.run_once()
    assert ctxs[0]._recheck_cursor == 2  # wraps: (8+4) % 10


# ----------------------------------------------------------------------
# Multi-guild tests (plan §2)
# ----------------------------------------------------------------------


def test_daemon_multi_guild_scans_all():
    """Two guild contexts: each is scanned, each store gets its own feeds,
    and stats aggregate correctly across guilds."""
    feeds_a = [_make_feed("B_a1"), _make_feed("B_a2")]
    feeds_b = [_make_feed("B_b1")]

    ctx_a = _make_guild_context(feed_page=feeds_a, guild_id="111", name="A")
    ctx_b = _make_guild_context(feed_page=feeds_b, guild_id="222", name="B")
    ctx_a.store._feed_ids = set()
    ctx_b.store._feed_ids = set()
    ctx_a.media_downloader._seen = {"m_a1", "m_a2"}
    ctx_b.media_downloader._seen = {"m_b1"}
    ctx_a.store._comment_keys = {"ca1"}
    ctx_b.store._comment_keys = {"cb1", "cb2", "cb3"}

    stats: dict = {}
    daemon = Daemon([ctx_a, ctx_b], interval_sec=0.01, stats=stats)
    daemon.run_once()

    assert ctx_a.store.append_feed.call_count == 2
    assert ctx_b.store.append_feed.call_count == 1

    # scanned_feeds counts PER GUILD-SCAN (G5): 2 guilds → +2 per cycle.
    assert stats["scanned_feeds"] == 2

    assert set(stats["guilds"].keys()) == {"111", "222"}
    assert stats["guilds"]["111"]["feeds_count"] == 0
    assert stats["guilds"]["222"]["feeds_count"] == 0
    assert stats["guilds"]["111"]["media_count"] == 2
    assert stats["guilds"]["222"]["media_count"] == 1
    assert stats["guilds"]["111"]["comments_count"] == 1
    assert stats["guilds"]["222"]["comments_count"] == 3

    assert stats["feeds_count"] == 0
    assert stats["comments_count"] == 4  # 1 + 3
    assert stats["media_count"] == 3  # 2 + 1
    assert stats["last_scan_ts"] > 0


def test_daemon_multi_guild_resilient_to_failure():
    """B3: one guild throwing during scan must not stop the other."""
    ctx_good = _make_guild_context(
        feed_page=[_make_feed("B_g")], guild_id="999", name="good"
    )
    ctx_bad = _make_guild_context(guild_id="888", name="bad")
    ctx_bad.feeds_scraper.client.get_guild_channels.side_effect = RuntimeError(
        "boom"
    )

    stats: dict = {}
    daemon = Daemon([ctx_bad, ctx_good], interval_sec=0.01, stats=stats)
    daemon.run_once()  # must NOT propagate the exception

    ctx_good.store.append_feed.assert_called_once_with(_make_feed("B_g"))
    assert "888" not in stats["guilds"]
    assert "999" in stats["guilds"]
    assert stats["scanned_feeds"] == 1


def test_daemon_multi_guild_recheck_cursor_independent():
    """G3: each guild's recheck cursor moves independently — they don't
    share a single rotation pointer."""
    old_a = [f"A_old_{i}" for i in range(100)]
    old_b = [f"B_old_{i}" for i in range(100)]

    ctx_a = _make_guild_context(guild_id="111", old_ids=old_a)
    ctx_b = _make_guild_context(guild_id="222", old_ids=old_b)
    ctx_a._recheck_batch_size = 10
    ctx_b._recheck_batch_size = 20

    stats: dict = {}
    daemon = Daemon([ctx_a, ctx_b], interval_sec=0.01, stats=stats)
    daemon.run_once()

    # G3: Cursors moved by their own batch sizes — NOT a shared value.
    assert ctx_a._recheck_cursor == 10
    assert ctx_b._recheck_cursor == 20

    daemon.run_once()
    assert ctx_a._recheck_cursor == 20
    assert ctx_b._recheck_cursor == 40
