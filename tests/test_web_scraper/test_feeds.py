"""Tests for src/web_scraper/feeds.py — FeedsScraper pagination + filtering."""

import os
import sys

from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.feeds import FeedsScraper

CHANNEL_ID = "7743321643036658"


def _make_feed(feed_id, guild_id=None):
    feed = {"id": feed_id, "createTime": 1234567890, "title": f"feed {feed_id}"}
    if guild_id is not None:
        feed["channelInfo"] = {"sign": {"guild_id": guild_id}}
    return feed


def _make_client(pages):
    """pages = list of (vecFeed, attchInfo, isFinish) tuples."""
    client = MagicMock()
    client.get_feeds.side_effect = pages
    return client


def _make_store(new_ids=None):
    """Store that returns True for IDs not in new_ids (default: all new)."""
    store = MagicMock()
    new_ids = new_ids or set()
    store.append_feed.side_effect = lambda f: f.get("id") not in new_ids
    return store


def test_scrape_all_paginates_3_pages():
    """3 pages: 10 + 10 + 3 feeds = 23 total, all new."""
    page1 = [_make_feed(f"B_{i}") for i in range(10)]
    page2 = [_make_feed(f"B_{i}") for i in range(10, 20)]
    page3 = [_make_feed(f"B_{i}") for i in range(20, 23)]
    client = _make_client([
        (page1, "p1", False),
        (page2, "p2", False),
        (page3, "p3", True),
    ])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 23
    assert client.get_feeds.call_count == 3
    assert store.append_feed.call_count == 23


def test_scrape_all_stops_on_finish():
    """A single page with isFinish=True ends the loop immediately."""
    page = [_make_feed("B_only")]
    client = _make_client([(page, "done", True)])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 1
    assert client.get_feeds.call_count == 1


def test_scrape_all_stops_on_empty_page():
    """An empty vecFeed without isFinish should still terminate the loop."""
    client = _make_client([([], "", False)])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 0
    assert client.get_feeds.call_count == 1


def test_guild_id_filter_skips_other_channels():
    """Feed with a different guild_id must NOT be passed to the store."""
    feed_other = _make_feed("B_other", guild_id="OTHER_GUILD")
    client = _make_client([([feed_other], "", True)])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 0
    store.append_feed.assert_not_called()


def test_guild_id_filter_allows_matching_channel():
    """Feed whose guild_id matches channel_id IS passed to the store."""
    feed_match = _make_feed("B_match", guild_id=CHANNEL_ID)
    client = _make_client([([feed_match], "", True)])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 1
    store.append_feed.assert_called_once_with(feed_match)


def test_guild_id_filter_allows_missing_sign():
    """Feed without channelInfo.sign.guild_id IS passed to the store.

    Matches inject.js:139 — only filter when guild_id is PRESENT and different.
    """
    feed_no_sign = _make_feed("B_nosign")
    client = _make_client([([feed_no_sign], "", True)])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 1
    store.append_feed.assert_called_once_with(feed_no_sign)


def test_feed_without_id_skipped():
    """Feed missing 'id' must be skipped (not stored)."""
    feed_no_id = {"createTime": 1234567890, "title": "no id"}
    client = _make_client([([feed_no_id], "", True)])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 0
    store.append_feed.assert_not_called()


def test_feed_without_createTime_skipped():
    """Feed missing 'createTime' must be skipped (not stored)."""
    feed_no_time = {"id": "B_notime", "title": "no time"}
    client = _make_client([([feed_no_time], "", True)])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 0
    store.append_feed.assert_not_called()


def test_dedup_via_store():
    """If store.append_feed returns False (duplicate), total_new must not count it."""
    feed = _make_feed("B_dup")
    client = _make_client([([feed], "", True)])
    store = _make_store(new_ids={"B_dup"})
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    total_new = scraper.scrape_all()

    assert total_new == 0
    store.append_feed.assert_called_once_with(feed)


def test_scrape_latest_single_page():
    """scrape_latest fetches exactly one page and returns the new count."""
    feeds = [_make_feed(f"B_l{i}") for i in range(5)]
    client = _make_client([(feeds, "cursor", False)])
    store = _make_store()
    scraper = FeedsScraper(client, store, CHANNEL_ID)

    new_count = scraper.scrape_latest()

    assert new_count == 5
    assert client.get_feeds.call_count == 1
    client.get_feeds.assert_called_once_with(7, "")
    assert store.append_feed.call_count == 5
