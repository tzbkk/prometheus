"""Data persistence layer for the web scraper.

Manages feeds.jsonl, comments.jsonl, ids.json, and comment_keys.json.
All writes are atomic (temp + rename) and thread-safe (threading.Lock).
Format is backward-compatible with the legacy inject.js scraper.
"""

import json
import threading
from pathlib import Path


class Store:
    """Thread-safe storage for feeds and comments with deduplication."""

    def __init__(self, data_dir):
        """Initialize store with data directory.

        Args:
            data_dir: Path to data directory (will be created if needed)
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._feed_ids: set[str] = set()
        self._comment_keys: set[str] = set()
        self._comments_fetched: dict[str, int] = {}
        self._load_existing()

    def _load_existing(self):
        """Load existing IDs from ids.json and comment_keys.json."""
        # Load feed IDs
        ids_file = self.data_dir / "ids.json"
        if ids_file.exists():
            try:
                for line in ids_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        self._feed_ids.add(line)
            except Exception:
                # File might be corrupt or empty; start fresh
                pass

        # Load comment keys
        keys_file = self.data_dir / "comment_keys.json"
        if keys_file.exists():
            try:
                for line in keys_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        self._comment_keys.add(line)
            except Exception:
                # File might be corrupt or empty; start fresh
                pass

        # Load comments-fetched feed IDs with last-known comment count.
        # Format: "feed_id\tcount" per line (tab-separated).
        # Backward-compat: lines without \t treated as count=0.
        fetched_file = self.data_dir / "comments_fetched_ids.json"
        if fetched_file.exists():
            try:
                for line in fetched_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if "\t" in line:
                        fid, count_str = line.split("\t", 1)
                        try:
                            self._comments_fetched[fid] = int(count_str)
                        except (ValueError, TypeError):
                            self._comments_fetched[fid] = 0
                    else:
                        self._comments_fetched[line] = 0
            except Exception:
                pass

    def append_feed(self, feed_dict) -> bool:
        """Append a feed to storage with deduplication.

        Args:
            feed_dict: Feed dictionary (must have 'id' field)

        Returns:
            True if feed was written (new), False if duplicate or invalid
        """
        feed_id = feed_dict.get("id", "")
        if not feed_id or feed_id in self._feed_ids:
            return False

        with self._lock:
            # Double-check under lock
            if feed_id in self._feed_ids:
                return False

            # Write to feeds.jsonl (atomic append)
            self._atomic_append_jsonl("feeds.jsonl", feed_dict)

            # Write to ids.json (atomic append)
            self._atomic_append_text("ids.json", feed_id)

            # Update in-memory set
            self._feed_ids.add(feed_id)

        return True

    def append_comment(self, comment_dict) -> bool:
        """Append a comment to storage with deduplication.

        Args:
            comment_dict: Comment dictionary with nested structure

        Returns:
            True if comment was written (new), False if duplicate or invalid
        """
        key = self._compute_comment_key(comment_dict)
        if key is None or key in self._comment_keys:
            return False

        with self._lock:
            # Double-check under lock
            if key in self._comment_keys:
                return False

            # Write to comments.jsonl (atomic append)
            self._atomic_append_jsonl("comments.jsonl", comment_dict)

            # Write to comment_keys.json (atomic append)
            self._atomic_append_text("comment_keys.json", key)

            # Update in-memory set
            self._comment_keys.add(key)

        return True

    def is_feed_captured(self, feed_id) -> bool:
        """Check if a feed ID has been captured.

        Args:
            feed_id: Feed ID to check

        Returns:
            True if feed exists in storage
        """
        return feed_id in self._feed_ids

    def is_comment_captured(self, comment_key) -> bool:
        """Check if a comment key has been captured.

        Args:
            comment_key: Comment key to check

        Returns:
            True if comment exists in storage
        """
        return comment_key in self._comment_keys

    def mark_comments_fetched(self, feed_id: str, comment_count: int = 0) -> None:
        """Record that comments have been fetched for a feed at the given count."""
        prev = self._comments_fetched.get(feed_id, -1)
        if comment_count <= prev:
            return
        with self._lock:
            if comment_count <= self._comments_fetched.get(feed_id, -1):
                return
            self._comments_fetched[feed_id] = comment_count
            self._atomic_append_text("comments_fetched_ids.json", f"{feed_id}\t{comment_count}")

    def is_comments_fetched(self, feed_id: str) -> bool:
        return feed_id in self._comments_fetched

    def get_comment_count_last_fetched(self, feed_id: str) -> int:
        """Return the comment count from the last fetch, or -1 if never fetched."""
        return self._comments_fetched.get(feed_id, -1)

    def get_all_feed_ids_with_comments(self) -> list[str]:
        """Return all feed IDs stored that have (or had) commentCount > 0."""
        ids = []
        feeds_file = self.data_dir / "feeds.jsonl"
        if not feeds_file.exists():
            return ids
        seen = set()
        for line in feeds_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                feed = json.loads(line)
                fid = feed.get("id", "")
                if not fid or fid in seen:
                    continue
                seen.add(fid)
                cc = feed.get("commentCount", 0)
                try:
                    cc = int(cc)
                except (TypeError, ValueError):
                    cc = 0
                if cc > 0:
                    ids.append(fid)
            except (json.JSONDecodeError, Exception):
                pass
        return ids

    def _atomic_append_jsonl(self, filename, obj):
        """Atomically append a JSON object to a JSONL file.

        Uses append mode which is atomic for single-line writes on POSIX.
        """
        filepath = self.data_dir / filename
        with open(filepath, mode="a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()

    def _atomic_append_text(self, filename, text):
        """Atomically append text to a file.

        Uses append mode which is atomic for single-line writes on POSIX.
        """
        filepath = self.data_dir / filename
        with open(filepath, mode="a", encoding="utf-8") as f:
            f.write(text + "\n")
            f.flush()

    @staticmethod
    def _compute_comment_key(comment_dict):
        """Compute deduplication key for a comment.

        Matches inject.js:145-157 computeCommentKey() pattern:
        - Extract vecComment from nested structure
        - Sort all comment IDs
        - Join with comma

        Args:
            comment_dict: Comment dictionary

        Returns:
            Sorted, comma-separated IDs, or None if no valid comments
        """
        try:
            d = comment_dict.get("d", {})
            vc = None

            # Try different paths to find vecComment (matching inject.js pattern)
            if d.get("d", {}).get("data") and isinstance(d["d"]["data"].get("vecComment"), list):
                vc = d["d"]["data"]["vecComment"]
            elif isinstance(d.get("vecComment"), list):
                vc = d["vecComment"]
            elif d.get("found") and isinstance(d["found"], list) and len(d["found"]) > 0:
                if isinstance(d["found"][0].get("vecComment"), list):
                    vc = d["found"][0]["vecComment"]

            if not vc or len(vc) == 0:
                return None

            # Extract IDs, filter empty ones, sort
            ids = [c.get("id", "") for c in vc if c.get("id")]
            ids = [i for i in ids if i]  # Filter empty strings
            ids.sort()

            if len(ids) == 0:
                return None

            return ",".join(ids)
        except Exception:
            return None