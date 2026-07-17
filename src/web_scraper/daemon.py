"""Daemon loop: periodically rescans for new feeds, comments, and media.

Runs in a background thread alongside the API server. Each cycle iterates
over ALL configured guild contexts, scanning each one in turn:

1. For each guild context ``ctx``: call ``_scan_guild(ctx)`` which fetches
   new feeds via ``ctx.feeds_scraper.client``, downloads media, scrapes
   comments, and re-checks a round-robin batch of old feeds.
2. Aggregate per-guild counts into top-level ``stats`` totals.

Graceful shutdown on SIGTERM/SIGINT.

The daemon and API server share a ``stats`` dict (daemon writes, API reads)
and per-guild ``store`` objects (thread-safe via ``threading.Lock`` from
Task 3). The daemon therefore never blocks the API thread.
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
    """Periodic rescanner that drives feeds/comments/media scrapers.

    Multi-guild (plan §2): the daemon holds a list of GuildContext objects
    and scans each one sequentially per cycle. Per-guild recheck state
    (round-robin cursor / batch / workers) lives on GuildContext so each
    guild maintains its own rotation independent of the others (G3).
    """

    def __init__(
        self,
        guild_contexts: list,
        interval_sec: float = 120,
        stats: dict[str, Any] | None = None,
    ):
        """Bind the daemon to its guild contexts.

        Args:
            guild_contexts: list of GuildContext-like objects, each exposing
                ``guild``, ``client``, ``store``, ``feeds_scraper``,
                ``comments_scraper``, ``media_downloader`` plus the
                per-guild recheck fields ``_recheck_cursor``,
                ``_recheck_batch_size``, ``_recheck_workers`` (G3).
            interval_sec: Seconds between scan cycles. Default 120.
            stats: Shared dict written each cycle. If None, a fresh dict
                is created. Existing keys are preserved via ``setdefault``.
        """
        self.guild_contexts = guild_contexts
        self.interval_sec = interval_sec
        self.stats: dict[str, Any] = stats if stats is not None else {}
        self.stats.setdefault("scanned_feeds", 0)
        self.stats.setdefault("feeds_count", 0)
        self.stats.setdefault("comments_count", 0)
        self.stats.setdefault("media_count", 0)
        self.stats.setdefault("last_scan_ts", 0)
        self.stats.setdefault("daemon_running", False)
        self.stats.setdefault("guilds", {})

        self._stop_event = threading.Event()
        self._cycle = 0
        self._log = logging.getLogger(__name__)

    def run_once(self) -> dict[str, Any]:
        """Execute one scan cycle across ALL guild contexts.

        Each guild is scanned in its own try/except so one failing guild
        does not abort the others (B3 resilient). After all guilds are
        scanned, per-guild counts are aggregated into top-level totals.
        ``daemon_running`` is True for the duration of the whole cycle
        and is always cleared in the ``finally`` block.
        """
        self.stats["daemon_running"] = True
        self._cycle += 1
        try:
            for ctx in self.guild_contexts:
                try:
                    self._scan_guild(ctx)
                except Exception:
                    self._log.exception(
                        "scan failed for guild %s", ctx.guild.guild_id
                    )
            self._aggregate_stats()
        finally:
            self.stats["daemon_running"] = False

        return self.stats

    def _scan_guild(self, ctx) -> None:
        """Scan a single guild context: feeds → media → comments → recheck.

        Updates ``self.stats["scanned_feeds"]`` (one increment per
        successful guild scan, so a full cycle with N guilds increments
        by N — G5) and writes per-guild counts to
        ``self.stats["guilds"][guild_id]``.
        """
        new_feeds = 0
        media_count = 0
        new_comments = 0

        all_feeds: list = []

        # Collect feeds from ALL guild channels via GetChannelTimelineFeeds.
        # pd.qq.com uses this endpoint for each channel tab; GetGuildFeeds
        # alone only returns the default channel (帖子广场).
        channels = ctx.feeds_scraper.client.get_guild_channels()
        if not channels:
            channels = [
                {"channel_id": ctx.feeds_scraper.channel_id, "name": "default"}
            ]

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
                        ctx.feeds_scraper.client.get_channel_feeds(
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
                    if not ctx.feeds_scraper._accepts(feed):
                        continue
                    all_feeds.append(feed)
                    if ctx.store.append_feed(feed):
                        new_feeds += 1
                        page_new += 1
                    try:
                        media_count += ctx.media_downloader.download_feed_media(feed)
                    except Exception:
                        self._log.exception(
                            "media download failed for feed=%s", feed.get("id")
                        )

                ch_pages += 1
                if page_new == 0 or finish or not ch_attch:
                    break

        # Fetch comments for feeds whose live commentCount exceeds what we
        # last saw (catches new comments on recent posts).
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
            last = ctx.store.get_comment_count_last_fetched(fid)
            if cc > last or last < 0:
                live_feeds.append(feed)

        if live_feeds:
            try:
                new_comments += ctx.comments_scraper.scrape_all(live_feeds)
            except Exception:
                self._log.exception("comments scrape failed")

        # Mark feeds with their live commentCount.
        for feed in all_feeds:
            fid = feed.get("id", "")
            if fid and ctx.store.is_feed_captured(fid):
                cc = feed.get("commentCount", 0)
                try:
                    cc = int(cc)
                except (TypeError, ValueError):
                    cc = 0
                ctx.store.mark_comments_fetched(fid, cc)

        # Re-check a small round-robin batch of old feeds for comment growth
        # (feeds no longer visible in the API's ~4-month window).
        # G3: cursor/batch/workers live on the guild context so each guild
        # rotates through its own feed-id array independently.
        old_ids = ctx.store.get_all_feed_ids_with_comments()
        if old_ids:
            n = len(old_ids)
            batch = min(ctx._recheck_batch_size, n)
            start = ctx._recheck_cursor % n
            batch_ids = [old_ids[(start + i) % n] for i in range(batch)]
            ctx._recheck_cursor = (start + batch) % n

            recheck = [{"id": fid, "commentCount": 1} for fid in batch_ids]
            try:
                new_comments += ctx.comments_scraper.scrape_all(
                    recheck, max_workers=ctx._recheck_workers
                )
            except Exception:
                self._log.exception("old feed comment re-check failed")

        # Per-guild stats block + G5: scanned_feeds counts PER GUILD-SCAN
        # (only on successful scans — exceptions propagate to run_once
        # which logs + swallows, leaving the counter untouched).
        self.stats.setdefault("guilds", {})[ctx.guild.guild_id] = {
            "feeds_count": len(getattr(ctx.store, "_feed_ids", set())),
            "comments_count": len(getattr(ctx.store, "_comment_keys", set())),
            "media_count": len(getattr(ctx.media_downloader, "_seen", set())),
            "last_scan_ts": int(time.time()),
        }
        self.stats["scanned_feeds"] = int(self.stats.get("scanned_feeds", 0)) + 1

        self._log.info(
            "Scan complete for guild %s: %d new feeds, %d new comments, %d media",
            ctx.guild.guild_id,
            new_feeds,
            new_comments,
            media_count,
        )

        try:
            self._write_state(ctx)
        except Exception:
            self._log.debug("state.json write skipped", exc_info=True)

    def _aggregate_stats(self) -> None:
        """Sum per-guild counts into top-level totals.

        Top-level ``feeds_count`` / ``comments_count`` / ``media_count``
        become the sum across all guilds. ``last_scan_ts`` becomes the
        most-recent per-guild scan ts. ``scanned_feeds`` is already
        incremented per-guild-scan inside ``_scan_guild`` (G5) and is
        NOT touched here.
        """
        guilds = self.stats.get("guilds", {})
        self.stats["feeds_count"] = sum(
            g.get("feeds_count", 0) for g in guilds.values()
        )
        self.stats["comments_count"] = sum(
            g.get("comments_count", 0) for g in guilds.values()
        )
        self.stats["media_count"] = sum(
            g.get("media_count", 0) for g in guilds.values()
        )
        self.stats["last_scan_ts"] = max(
            (g.get("last_scan_ts", 0) for g in guilds.values()),
            default=int(time.time()),
        )

    def _write_state(self, ctx) -> None:
        """Write per-guild state.json (G4: takes ctx, uses ctx.store.data_dir)."""
        feeds_file = ctx.store.data_dir / "feeds.jsonl"
        state_file = ctx.store.data_dir / "state.json"
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
            "feeds": len(ctx.store._feed_ids),
            "comments": len(ctx.store._comment_keys),
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
