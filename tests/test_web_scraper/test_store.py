import json
import os
import sys
import tempfile
import threading

from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.store import Store


def test_append_feed_writes_to_jsonl(tmp_path):
    """Append a feed and verify it's written to feeds.jsonl."""
    store = Store(tmp_path)
    feed = {"id": "B_test1", "title": "hello"}
    result = store.append_feed(feed)
    assert result is True

    lines = (tmp_path / "feeds.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "B_test1"


def test_append_feed_dedup(tmp_path):
    """Append same feed twice - only first should be written."""
    store = Store(tmp_path)
    feed = {"id": "B_test1", "title": "hello"}

    result1 = store.append_feed(feed)
    assert result1 is True

    result2 = store.append_feed(feed)
    assert result2 is False

    lines = (tmp_path / "feeds.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1


def test_append_feed_no_id_skipped(tmp_path):
    """Feed without ID should be skipped."""
    store = Store(tmp_path)
    feed = {"title": "hello"}

    result = store.append_feed(feed)
    assert result is False

    assert not (tmp_path / "feeds.jsonl").exists()


def test_is_feed_captured(tmp_path):
    """Check is_feed_captured after appending."""
    store = Store(tmp_path)
    feed = {"id": "B_test1", "title": "hello"}

    assert store.is_feed_captured("B_test1") is False
    store.append_feed(feed)
    assert store.is_feed_captured("B_test1") is True


def test_load_existing_ids(tmp_path):
    """Pre-create ids.json with IDs and verify they're loaded."""
    # Pre-populate ids.json
    ids_file = tmp_path / "ids.json"
    ids_file.write_text("B_existing1\nB_existing2\n", encoding="utf-8")

    store = Store(tmp_path)

    assert store.is_feed_captured("B_existing1") is True
    assert store.is_feed_captured("B_existing2") is True
    assert store.is_feed_captured("B_new") is False

    # Appending existing ID should fail
    feed = {"id": "B_existing1", "title": "test"}
    assert store.append_feed(feed) is False


def test_append_comment_writes_to_jsonl(tmp_path):
    """Append a comment and verify it's written to comments.jsonl."""
    store = Store(tmp_path)
    comment = {
        "_s": "scroll",
        "ts": 1234567890,
        "d": {
            "feedId": "B_test1",
            "totalNum": 2,
            "vecComment": [
                {"id": "c1", "content": "first"},
                {"id": "c2", "content": "second"},
            ],
        },
    }

    result = store.append_comment(comment)
    assert result is True

    lines = (tmp_path / "comments.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    loaded = json.loads(lines[0])
    assert loaded["d"]["feedId"] == "B_test1"


def test_append_comment_dedup(tmp_path):
    """Same comment key twice - only first should be written."""
    store = Store(tmp_path)
    comment = {
        "_s": "scroll",
        "ts": 1234567890,
        "d": {
            "feedId": "B_test1",
            "vecComment": [
                {"id": "c1", "content": "first"},
                {"id": "c2", "content": "second"},
            ],
        },
    }

    result1 = store.append_comment(comment)
    assert result1 is True

    # Even with different timestamp, same comment IDs should dedup
    comment["ts"] = 9999999999
    result2 = store.append_comment(comment)
    assert result2 is False

    lines = (tmp_path / "comments.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1


def test_compute_comment_key_sorts_ids():
    """Comment key should sort IDs and join with comma."""
    comment = {
        "d": {
            "vecComment": [
                {"id": "c_z"},
                {"id": "c_a"},
                {"id": "c_m"},
            ]
        }
    }

    key = Store._compute_comment_key(comment)
    assert key == "c_a,c_m,c_z"


def test_compute_comment_key_empty_returns_none():
    """Comment with empty vecComment should return None."""
    comment = {"d": {"vecComment": []}}

    key = Store._compute_comment_key(comment)
    assert key is None


def test_compute_comment_key_nested_paths():
    """Test all three vecComment path patterns from inject.js."""
    # Pattern 1: d.d.data.vecComment
    comment1 = {
        "d": {
            "d": {
                "data": {
                    "vecComment": [{"id": "c1"}, {"id": "c2"}]
                }
            }
        }
    }
    key1 = Store._compute_comment_key(comment1)
    assert key1 == "c1,c2"

    # Pattern 2: d.vecComment
    comment2 = {
        "d": {
            "vecComment": [{"id": "c3"}, {"id": "c4"}]
        }
    }
    key2 = Store._compute_comment_key(comment2)
    assert key2 == "c3,c4"

    # Pattern 3: d.found[0].vecComment
    comment3 = {
        "d": {
            "found": [
                {
                    "vecComment": [{"id": "c5"}, {"id": "c6"}]
                }
            ]
        }
    }
    key3 = Store._compute_comment_key(comment3)
    assert key3 == "c5,c6"


def test_concurrent_append_no_corruption(tmp_path):
    """10 threads each append 10 unique feeds - verify 100 lines, no corruption."""
    store = Store(tmp_path)
    num_threads = 10
    feeds_per_thread = 10

    def append_feeds(thread_id):
        for i in range(feeds_per_thread):
            feed_id = f"B_t{thread_id}_f{i}"
            feed = {"id": feed_id, "title": f"feed_{i}"}
            store.append_feed(feed)

    threads = []
    for t in range(num_threads):
        thread = threading.Thread(target=append_feeds, args=(t,))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    # Verify exactly 100 lines in feeds.jsonl
    lines = (tmp_path / "feeds.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == num_threads * feeds_per_thread

    # Verify all lines are valid JSON and have unique IDs
    seen_ids = set()
    for line in lines:
        feed = json.loads(line)
        assert feed["id"] not in seen_ids
        seen_ids.add(feed["id"])

    assert len(seen_ids) == 100


def test_concurrent_append_comments(tmp_path):
    """Concurrent comment appends - verify no corruption, correct count."""
    store = Store(tmp_path)
    num_threads = 10
    comments_per_thread = 10

    def append_comments(thread_id):
        for i in range(comments_per_thread):
            comment = {
                "_s": "scroll",
                "ts": 1234567890 + thread_id * 1000 + i,
                "d": {
                    "feedId": f"B_test{i}",
                    "vecComment": [
                        {"id": f"c_t{thread_id}_{i}_1"},
                        {"id": f"c_t{thread_id}_{i}_2"},
                    ],
                },
            }
            store.append_comment(comment)

    threads = []
    for t in range(num_threads):
        thread = threading.Thread(target=append_comments, args=(t,))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    # Verify exactly 100 lines in comments.jsonl
    lines = (tmp_path / "comments.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == num_threads * comments_per_thread

    # Verify all lines are valid JSON
    for line in lines:
        comment = json.loads(line)
        assert "d" in comment
        assert "vecComment" in comment["d"] or "d" in comment["d"]


def test_comment_key_filter_empty_ids():
    """Comment with some empty IDs should filter them out."""
    comment = {
        "d": {
            "vecComment": [
                {"id": "c1"},
                {"id": ""},
                {"id": "c2"},
                {"content": "no id field"},
            ]
        }
    }

    key = Store._compute_comment_key(comment)
    # Should only include c1 and c2, sorted
    assert key == "c1,c2"


def test_load_existing_comment_keys(tmp_path):
    """Pre-create comment_keys.json and verify they're loaded."""
    # Pre-populate comment_keys.json
    keys_file = tmp_path / "comment_keys.json"
    keys_file.write_text("c1,c2\nx,y,z\n", encoding="utf-8")

    store = Store(tmp_path)

    assert store.is_comment_captured("c1,c2") is True
    assert store.is_comment_captured("x,y,z") is True

    # Appending with existing key should fail
    comment = {
        "d": {
            "vecComment": [{"id": "c1"}, {"id": "c2"}]
        }
    }
    assert store.append_comment(comment) is False


def test_mark_comments_fetched(tmp_path):
    store = Store(tmp_path)
    assert store.is_comments_fetched("B_a") is False
    store.mark_comments_fetched("B_a", 5)
    assert store.is_comments_fetched("B_a") is True
    # Marking with lower count is ignored
    store.mark_comments_fetched("B_a", 3)
    assert store.is_comments_fetched("B_a") is True


def test_comments_fetched_persisted(tmp_path):
    store = Store(tmp_path)
    store.mark_comments_fetched("B_x", 3)
    store.mark_comments_fetched("B_y", 7)
    store2 = Store(tmp_path)
    assert store2.is_comments_fetched("B_x") is True
    assert store2.is_comments_fetched("B_y") is True
    assert store2.is_comments_fetched("B_z") is False


def test_get_comment_count_last_fetched(tmp_path):
    store = Store(tmp_path)
    assert store.get_comment_count_last_fetched("B_x") == -1
    store.mark_comments_fetched("B_x", 5)
    assert store.get_comment_count_last_fetched("B_x") == 5


def test_get_all_feed_ids_with_comments(tmp_path):
    store = Store(tmp_path)
    store.append_feed({"id": "B_c5", "createTime": 1, "commentCount": 5})
    store.append_feed({"id": "B_c0", "createTime": 2, "commentCount": 0})
    store.append_feed({"id": "B_c3", "createTime": 3, "commentCount": 3})
    store.append_feed({"id": "B_c5", "createTime": 4, "commentCount": 8})
    ids = store.get_all_feed_ids_with_comments()
    assert sorted(ids) == ["B_c3", "B_c5"]