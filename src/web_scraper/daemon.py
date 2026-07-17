"""Daemon loop: periodically rescans for new feeds, comments, and media.

Runs in a background thread alongside the API server. Each cycle:
1. scrape_latest() to fetch new feeds
2. For new feeds: download media + scrape comments
3. Update shared stats dict for API server exposure

Graceful shutdown on SIGTERM/SIGINT.

The daemon and API server share a ``stats`` dict (daemon writes, API reads)
and a ``store`` (thread-safe via ``threading.Lock`` from Task 3). The
daemon therefore never blocks the API thread.
"""

from __future__ import annotations

import hashlib
import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class Daemon:
    """Periodic rescanner that drives feeds/comments/media scrapers."""

    def __init__(
        self,
        feeds_scraper,
        comments_scraper,
        media_downloader,
        store,
        interval_sec: float = 120,
        stats: dict[str, Any] | None = None,
    ):
        """Bind the daemon to its dependencies.

        Args:
            feeds_scraper: FeedsScraper-like object. Must expose
                ``client.get_feeds(from_, attch_info)`` and ``_accepts(feed)``.
                The daemon talks to ``client`` directly so it can capture
                the feed objects (``scrape_latest`` returns a count only).
            comments_scraper: CommentsScraper-like object exposing
                ``scrape_all(feeds) -> int``.
            media_downloader: MediaDownloader-like object exposing
                ``download_feed_media(feed) -> int``.
            store: thread-safe Store. Read for live counts
                (``_feed_ids`` / ``_comment_keys``) and called via
                ``append_feed`` for dedup.
            interval_sec: Seconds between scan cycles. Default 120.
            stats: Shared dict written each cycle. If None, a fresh dict
                is created. Existing keys are preserved via ``setdefault``.
        """
        self.feeds_scraper = feeds_scraper
        self.comments_scraper = comments_scraper
        self.media_downloader = media_downloader
        self.store = store
        self.interval_sec = interval_sec
        self.stats: dict[str, Any] = stats if stats is not None else {}
        self.stats.setdefault("scanned_feeds", 0)
        self.stats.setdefault("feeds_count", 0)
        self.stats.setdefault("comments_count", 0)
        self.stats.setdefault("media_count", 0)
        self.stats.setdefault("last_scan_ts", 0)
        self.stats.setdefault("daemon_running", False)

        self._stop_event = threading.Event()
        self._cycle = 0
        # Round-robin re-check: process a small batch of old feeds each cycle
        # instead of all at once. 6000+ concurrent requests cause QQ API to
        # rate-limit (SSL handshake timeouts). At 50/cycle × 120s interval,
        # full rotation takes ~4 hours — gentle enough for QQ's servers.
        self._recheck_batch_size = 50
        self._recheck_cursor = 0
        self._recheck_workers = 3
        self._log = logging.getLogger(__name__)

    def run_once(self) -> dict[str, Any]:
        """Execute a single scan cycle and return the shared ``stats`` dict.

        Pulls one feed page directly from ``feeds_scraper.client`` so we
        have feed OBJECTS (not just a count returned by
        ``scrape_latest``). Each accepted feed is deduped via the store,
        has its media downloaded, and is passed to the comments scraper.
        ``daemon_running`` is True for the duration of the cycle and is
        always cleared in the ``finally`` block.
        """
        self.stats["daemon_running"] = True
        self._cycle += 1
        try:
            new_feeds = 0
            media_count = 0
            new_comments = 0

            all_feeds: list = []
            attch_info = ""

            # Collect feeds from ALL guild channels via GetChannelTimelineFeeds.
            # pd.qq.com uses this endpoint for each channel tab; GetGuildFeeds
            # alone only returns the default channel (帖子广场).
            channels = self.feeds_scraper.client.get_guild_channels()
            if not channels:
                channels = [{"channel_id": self.feeds_scraper.channel_id, "name": "default"}]

            for ch in channels:
                ch_id = str(ch.get("channel_id", ""))
                ch_name = ch.get("name", "?")
                if not ch_id:
                    continue
                ch_attch = ""
                ch_pages = 0
                _MAX_CH_PAGES = 50
                while ch_pages < _MAX_CH_PAGES:
                    try:
                        vec_feed, ch_attch, finish = (
                            self.feeds_scraper.client.get_channel_feeds(
                                ch_id, 7, ch_attch
                            )
                        )
                    except Exception:
                        self._log.exception(
                            "channel feed fetch failed for %s", ch_name
                        )
                        break
                    if not vec_feed:
                        break

                    page_new = 0
                    for feed in vec_feed:
                        if not self.feeds_scraper._accepts(feed):
                            continue
                        all_feeds.append(feed)
                        if self.store.append_feed(feed):
                            new_feeds += 1
                            page_new += 1
                        try:
                            media_count += self.media_downloader.download_feed_media(feed)
                        except Exception:
                            self._log.exception(
                                "media download failed for feed=%s", feed.get("id")
                            )

                    ch_pages += 1
                    if page_new == 0 or finish or not ch_attch:
                        break

            # Fetch comments for feeds whose live commentCount
            # exceeds what we last saw (catches new comments on recent posts).
            live_feeds = []
            for feed in all_feeds:
                fid = feed.get("id", "")
                cc = feed.get("commentCount", 0)
                try:
                    cc = int(cc)
                except (TypeError, ValueError):
                    cc = 0
                if cc <= 0:
                    continue
                last = self.store.get_comment_count_last_fetched(fid)
                if cc > last or last < 0:
                    live_feeds.append(feed)

            if live_feeds:
                try:
                    new_comments += self.comments_scraper.scrape_all(live_feeds)
                except Exception:
                    self._log.exception("comments scrape failed")

            # Mark feeds with their live commentCount.
            for feed in all_feeds:
                fid = feed.get("id", "")
                if fid and self.store.is_feed_captured(fid):
                    cc = feed.get("commentCount", 0)
                    try:
                        cc = int(cc)
                    except (TypeError, ValueError):
                        cc = 0
                    self.store.mark_comments_fetched(fid, cc)

            # Re-check a small round-robin batch of old feeds for comment
            # growth (feeds no longer visible in the API's ~4-month window).
            old_ids = self.store.get_all_feed_ids_with_comments()
            if old_ids:
                n = len(old_ids)
                batch = min(self._recheck_batch_size, n)
                start = self._recheck_cursor % n
                batch_ids = [old_ids[(start + i) % n] for i in range(batch)]
                self._recheck_cursor = (start + batch) % n

                recheck = [{"id": fid, "commentCount": 1} for fid in batch_ids]
                try:
                    new_comments += self.comments_scraper.scrape_all(
                        recheck, max_workers=self._recheck_workers
                    )
                except Exception:
                    self._log.exception("old feed comment re-check failed")

            self.stats["scanned_feeds"] = int(self.stats.get("scanned_feeds", 0)) + 1
            self.stats["last_scan_ts"] = int(time.time())
            self.stats["feeds_count"] = len(getattr(self.store, "_feed_ids", set()))
            self.stats["comments_count"] = len(
                getattr(self.store, "_comment_keys", set())
            )
            self.stats["media_count"] = len(getattr(self.media_downloader, "_seen", set()))

            self._log.info(
                "Scan complete: %d new feeds, %d new comments, %d media",
                new_feeds,
                new_comments,
                media_count,
            )

            try:
                self._write_state()
            except Exception:
                self._log.debug("state.json write skipped", exc_info=True)
        finally:
            self.stats["daemon_running"] = False

        return self.stats

    def _write_state(self) -> None:
        feeds_file = self.store.data_dir / "feeds.jsonl"
        state_file = self.store.data_dir / "state.json"
        try:
            h = hashlib.sha256()
            if feeds_file.exists():
                h.update(feeds_file.read_bytes())
            feeds_hash = hashlib.sha256(h.digest()).hexdigest()
        except OSError:
            feeds_hash = "missing"

        now_iso = datetime.now(timezone.utc).isoformat()
        state = {
            "bottomReached": True,
            "bottomTime": now_iso,
            "feeds": len(self.store._feed_ids),
            "comments": len(self.store._comment_keys),
            "hash": feeds_hash,
            "hashFiles": ["feeds.jsonl"],
            "hashTime": now_iso,
        }
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(state_file)

    def run_forever(self) -> None:
        """Run :meth:`run_once` every ``interval_sec`` until :meth:`stop`.

        Per-cycle exceptions are logged and swallowed so one bad scan
        cannot kill the loop. Sleeps via ``_stop_event.wait()`` so a
        stop signal wakes the loop immediately.
        """
        self._install_signal_handlers()
        self._log.info("Daemon started (interval=%ss)", self.interval_sec)
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                self._log.exception("Daemon cycle failed")
            self._stop_event.wait(self.interval_sec)
        self._log.info("Daemon stopped")

    def stop(self) -> None:
        """Signal the daemon loop to exit after the current cycle."""
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        """Wire SIGTERM/SIGINT to :meth:`_on_signal`.

        ``signal.signal`` may only be called from the main thread; if the
        daemon runs in a background thread we skip installation (the main
        thread is expected to forward signals via :meth:`stop`).
        """
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        except (ValueError, OSError):
            self._log.debug(
                "signal handlers not installed (not main thread); "
                "use stop() to terminate"
            )

    def _on_signal(self, signum, frame) -> None:
        """Signal handler: log and trip the stop event."""
        self._log.info("Received signal %d, stopping daemon", signum)
        self._stop_event.set()
