"""Feeds scraper: paginates GetGuildFeeds and stores new feeds.

Uses the QQWebClient to fetch pages of 10 feeds, filtering by channel_id
and writing new feeds to the Store. Pagination continues until isFinish=True.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class FeedsScraper:
    """Paginate GetGuildFeeds and persist new feeds via the Store."""

    def __init__(self, client, store, channel_id):
        """Bind the scraper to its dependencies.

        Args:
            client: Duck-typed QQWebClient — must expose
                ``get_feeds(from_, feed_attch_info) -> (vecFeed, attchInfo, isFinish)``.
            store: Duck-typed Store — must expose
                ``append_feed(feed_dict) -> bool`` (True if newly written).
            channel_id: Target channel id used for the guild_id filter.
        """
        self.client = client
        self.store = store
        self.channel_id = channel_id
        self._log = logging.getLogger(__name__)

    def _accepts(self, feed) -> bool:
        """Return True if ``feed`` should be written to the store."""
        if not feed.get("id") or not feed.get("createTime"):
            self._log.warning(
                "Skipping feed missing id/createTime: %r", feed.get("id")
            )
            return False

        # Matches inject.js:139 — only skip when guild_id is PRESENT and
        # DIFFERENT. Missing sign is allowed (cross-guild feeds w/o sign).
        channel_info = feed.get("channelInfo") or {}
        sign = channel_info.get("sign") or {}
        guild_id = sign.get("guild_id")
        if guild_id and guild_id != self.channel_id:
            return False

        return True

    def _ingest(self, vec_feed) -> int:
        """Filter + store a single page; return count of NEW feeds."""
        new_count = 0
        for feed in vec_feed:
            if not self._accepts(feed):
                continue
            if self.store.append_feed(feed):
                new_count += 1
        return new_count

    def scrape_all(self) -> int:
        """Page through GetGuildFeeds until ``isFinish`` or empty page.

        Returns:
            Total number of NEW feeds written across all pages.
        """
        attch_info = ""
        from_ = 7
        total_new = 0
        total_seen = 0
        page = 0

        while True:
            vec_feed, attch_info, is_finish = self.client.get_feeds(
                from_, attch_info
            )

            total_seen += len(vec_feed)
            total_new += self._ingest(vec_feed)

            page += 1
            if page % 10 == 0:
                self._log.info(
                    "Scraped %d pages, %d new feeds, %d total seen",
                    page,
                    total_new,
                    total_seen,
                )

            if is_finish:
                break
            if len(vec_feed) == 0:
                # Empty page without isFinish is still the end of data.
                break

        self._log.info(
            "Feeds scrape complete: %d page(s), %d new feeds, %d total seen",
            page,
            total_new,
            total_seen,
        )
        return total_new

    def scrape_latest(self) -> int:
        """Fetch a single page for incremental daemon mode.

        Does NOT paginate further — intended for short polling cycles
        where the daemon only needs to know whether new content exists.
        """
        vec_feed, _, _ = self.client.get_feeds(7, "")
        return self._ingest(vec_feed)

    def scrape_channel(self, channel_id: str, from_: int = 7) -> int:
        """Paginate one channel via ``GetChannelTimelineFeeds``.

        pd.qq.com uses this endpoint for channel-specific feeds (12 per
        page, ``isFinish`` when exhausted).  Returns the number of NEW
        feeds written.

        Args:
            channel_id: Numeric channel ID string.
            from_: Feed ordering (7 = latest/recommended).
        """
        attch_info = ""
        page = 0
        total_new = 0

        while True:
            vec_feed, attch_info, is_finish = self.client.get_channel_feeds(
                channel_id, from_, attch_info
            )
            total_new += self._ingest(vec_feed)
            page += 1

            if is_finish or not vec_feed or not attch_info:
                break
            if page >= 1 and total_new == 0:
                break

        return total_new
