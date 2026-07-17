"""Tests for src/web_scraper/comments.py — CommentsScraper.

All tests use mocked client+store — no network, no filesystem writes.
"""

import os
import sys
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.comments import CommentsScraper, _MAX_PAGES_PER_FEED


def _make_client(pages_per_feed=None):
    """Build a mock client whose get_feed_comments paginates per-feed.

    pages_per_feed = {feed_id: [(vecComment_list, totalNum_int, attchInfo_str), ...]}
    Each call advances to the next tuple; beyond the list, returns ([], 0, "").
    """
    pages_per_feed = pages_per_feed or {}
    client = MagicMock()
    call_counts: dict[str, int] = {}

    def get_comments(feed_id, attch_info=""):
        idx = call_counts.get(feed_id, 0)
        call_counts[feed_id] = idx + 1
        pages = pages_per_feed.get(feed_id, [])
        if idx < len(pages):
            return pages[idx]
        return ([], 0, "")

    client.get_feed_comments.side_effect = get_comments
    client._call_counts = call_counts
    return client


def _make_store(existing_keys=None, always_new=True):
    """Build a mock store. If always_new=False, every append returns False."""
    store = MagicMock()
    existing_keys = existing_keys or set()
    if always_new:
        store.append_comment.side_effect = lambda r: True
    else:
        store.append_comment.side_effect = lambda r: False
    return store


def _comment(cid):
    return {"id": cid, "content": f"comment-{cid}"}


def test_scrape_feed_comments_single_page():
    """One page of comments, empty attchInfo → 1 store call, returns 1."""
    client = _make_client({
        "B_1": [([_comment("c1"), _comment("c2")], 2, "")]
    })
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    result = scraper.scrape_feed_comments("B_1")

    assert result == 1
    assert client.get_feed_comments.call_count == 1
    assert store.append_comment.call_count == 1

    record = store.append_comment.call_args[0][0]
    assert record["d"]["feedId"] == "B_1"
    assert record["d"]["totalNum"] == 2
    assert len(record["d"]["vecComment"]) == 2


def test_scrape_feed_comments_multi_page():
    """Two pages of comments: page1 has cursor, page2 has empty cursor."""
    client = _make_client({
        "B_2": [
            ([_comment(f"c{i}") for i in range(20)], 25, "cursor-page2"),
            ([_comment(f"c{i}") for i in range(20, 25)], 25, ""),
        ]
    })
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    result = scraper.scrape_feed_comments("B_2")

    assert result == 2
    assert client.get_feed_comments.call_count == 2
    assert store.append_comment.call_count == 2


def test_scrape_feed_comments_empty_breaks():
    """Empty vecComment on first call → no store write, returns 0."""
    client = _make_client({"B_3": [([], 0, "")]})
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    result = scraper.scrape_feed_comments("B_3")

    assert result == 0
    assert client.get_feed_comments.call_count == 1
    store.append_comment.assert_not_called()


def test_scrape_feed_comments_dedup():
    """Store returns False (duplicate) → total_new stays 0."""
    client = _make_client({
        "B_4": [([_comment("c1")], 1, "")]
    })
    store = _make_store(always_new=False)
    scraper = CommentsScraper(client, store, "guild123")

    result = scraper.scrape_feed_comments("B_4")

    assert result == 0
    assert store.append_comment.call_count == 1


def test_scrape_feed_comments_safety_limit():
    """Infinite pagination (always returns data + cursor) must stop at 50 pages."""
    page = ([_comment("cx")], 1000, "cursor-forever")
    client = _make_client({"B_5": [page] * 1000})
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    result = scraper.scrape_feed_comments("B_5")

    assert client.get_feed_comments.call_count == _MAX_PAGES_PER_FEED
    assert result == _MAX_PAGES_PER_FEED
    assert store.append_comment.call_count == _MAX_PAGES_PER_FEED


def test_scrape_all_skips_zero_comments():
    """Feeds with commentCount == 0 must NOT trigger get_feed_comments."""
    client = _make_client({"B_10": [([_comment("c1")], 1, "")]})
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    feeds = [
        {"id": "B_10", "commentCount": 5},
        {"id": "B_11", "commentCount": 0},
    ]
    result = scraper.scrape_all(feeds)

    assert result == 1
    feed_ids_called = [c.args[0] for c in client.get_feed_comments.call_args_list]
    assert "B_11" not in feed_ids_called
    assert "B_10" in feed_ids_called


def test_scrape_all_parallel():
    """5 feeds, each with comments, should all be scraped in parallel."""
    pages = {
        f"B_{i}": [([_comment(f"c-{i}-1")], 1, "")] for i in range(20, 25)
    }
    client = _make_client(pages)
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123", max_workers=5)

    feeds = [{"id": f"B_{i}", "commentCount": 1} for i in range(20, 25)]
    result = scraper.scrape_all(feeds)

    assert result == 5
    feed_ids_called = {c.args[0] for c in client.get_feed_comments.call_args_list}
    assert feed_ids_called == {f"B_{i}" for i in range(20, 25)}


def test_comment_record_format():
    """Verify the stored record has the inject.js-compatible envelope."""
    client = _make_client({"B_30": [([_comment("c1")], 5, "")]})
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    scraper.scrape_feed_comments("B_30")

    record = store.append_comment.call_args[0][0]
    assert set(record.keys()) >= {"_s", "ts", "d"}
    assert record["_s"] == "web_api"
    assert isinstance(record["ts"], int)
    assert record["ts"] > 0
    assert set(record["d"].keys()) == {"feedId", "totalNum", "vecComment"}
    assert record["d"]["feedId"] == "B_30"
    assert record["d"]["totalNum"] == 5
    assert isinstance(record["d"]["vecComment"], list)
    assert record["d"]["vecComment"][0]["id"] == "c1"


def test_scrape_all_accepts_feed_dicts():
    """Accept list of feed dicts with {id, commentCount}."""
    client = _make_client({"B_40": [([_comment("c1")], 1, "")]})
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    feeds = [{"id": "B_40", "commentCount": 3}]
    result = scraper.scrape_all(feeds)

    assert result == 1


def test_scrape_all_accepts_tuples():
    """Accept list of (feed_id, comment_count) tuples."""
    client = _make_client({"B_50": [([_comment("c1")], 1, "")]})
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    feeds = [("B_50", 2)]
    result = scraper.scrape_all(feeds)

    assert result == 1
    feed_ids_called = [c.args[0] for c in client.get_feed_comments.call_args_list]
    assert feed_ids_called == ["B_50"]
