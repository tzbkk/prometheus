"""Integration tests for scripts/scraper_backfill.py pure functions."""
import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scraper_backfill import (
    discover_guilds,
    load_feeds,
    load_fully_fetched_feed_ids,
    _select_comment_targets,
)


class TestDiscoverGuilds:
    def test_finds_numeric_subdirs_with_feeds(self, tmp_path):
        """Numeric subdir with feeds.jsonl → discovered."""
        guild_dir = tmp_path / "7743321643036658"
        guild_dir.mkdir()
        (guild_dir / "feeds.jsonl").write_text('{"id": "B_test"}\n')
        result = discover_guilds(tmp_path)
        assert result == ["7743321643036658"]

    def test_ignores_non_numeric_dirs(self, tmp_path):
        """Non-numeric subdir → ignored."""
        (tmp_path / "some_name").mkdir()
        (tmp_path / "some_name" / "feeds.jsonl").write_text('{}\n')
        assert discover_guilds(tmp_path) == []

    def test_ignores_numeric_without_feeds(self, tmp_path):
        """Numeric subdir without feeds.jsonl → ignored."""
        (tmp_path / "123456").mkdir()
        assert discover_guilds(tmp_path) == []

    def test_sorted_ascending(self, tmp_path):
        """Multiple guilds → sorted ascending."""
        for gid in ["333", "111", "222"]:
            d = tmp_path / gid
            d.mkdir()
            (d / "feeds.jsonl").write_text('{}\n')
        assert discover_guilds(tmp_path) == ["111", "222", "333"]

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        assert discover_guilds(tmp_path / "nonexistent") == []


class TestLoadFeeds:
    def test_reads_valid_jsonl(self, tmp_path):
        feeds = [{"id": "B_1"}, {"id": "B_2"}]
        (tmp_path / "feeds.jsonl").write_text(
            "\n".join(json.dumps(f) for f in feeds) + "\n"
        )
        result = load_feeds(tmp_path)
        assert len(result) == 2
        assert result[0]["id"] == "B_1"

    def test_skips_malformed_lines(self, tmp_path):
        (tmp_path / "feeds.jsonl").write_text(
            '{"id": "B_1"}\n{malformed}\n{"id": "B_2"}\n'
        )
        result = load_feeds(tmp_path)
        assert len(result) == 2

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_feeds(tmp_path) == []

    def test_empty_file_returns_empty(self, tmp_path):
        (tmp_path / "feeds.jsonl").write_text("")
        result = load_feeds(tmp_path)
        assert result == []


class TestLoadFullyFetchedFeedIds:
    def test_reads_tab_separated(self, tmp_path):
        (tmp_path / "comments_fetched_ids.json").write_text(
            "B_111\t5\nB_222\t10\nB_333\t0\n"
        )
        result = load_fully_fetched_feed_ids(tmp_path)
        assert result == {"B_111", "B_222", "B_333"}

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_fully_fetched_feed_ids(tmp_path) == set()

    def test_dedupes_duplicate_entries(self, tmp_path):
        (tmp_path / "comments_fetched_ids.json").write_text(
            "B_111\t5\nB_111\t10\n"
        )
        result = load_fully_fetched_feed_ids(tmp_path)
        assert result == {"B_111"}

    def test_skips_empty_lines(self, tmp_path):
        (tmp_path / "comments_fetched_ids.json").write_text(
            "\nB_111\t5\n\n"
        )
        result = load_fully_fetched_feed_ids(tmp_path)
        assert result == {"B_111"}


class TestSelectCommentTargets:
    def test_selects_feeds_with_comments(self):
        feeds = [
            {"id": "B_1", "commentCount": 5},
            {"id": "B_2", "commentCount": 0},
            {"id": "B_3", "commentCount": 10},
        ]
        targets, skipped = _select_comment_targets(feeds, set())
        assert "B_1" in targets
        assert "B_3" in targets
        assert "B_2" not in targets
        assert skipped == 1

    def test_skips_already_done(self):
        feeds = [{"id": "B_1", "commentCount": 5}]
        targets, skipped = _select_comment_targets(feeds, {"B_1"})
        assert targets == []
        assert skipped == 1

    def test_skips_missing_id(self):
        feeds = [{"commentCount": 5}]
        targets, skipped = _select_comment_targets(feeds, set())
        assert targets == []
        assert skipped == 0

    def test_handles_non_numeric_comment_count(self):
        feeds = [{"id": "B_1", "commentCount": "invalid"}]
        targets, skipped = _select_comment_targets(feeds, set())
        assert targets == []
        assert skipped == 1

    def test_empty_feeds(self):
        targets, skipped = _select_comment_targets([], set())
        assert targets == []
        assert skipped == 0