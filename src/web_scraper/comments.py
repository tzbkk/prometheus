"""Comments scraper: fetches comments for feeds via GetFeedComments API.

Multi-threaded: uses ThreadPoolExecutor to fetch comments for multiple
feeds concurrently. Paginates within each feed until all comments retrieved.
Comment data is stored in the inject.js-compatible format.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Safety limit: prevent infinite pagination loops per feed.
_MAX_PAGES_PER_FEED = 50


class CommentsScraper:
    """Scrapes comments for feeds with multi-threaded pagination.

    Each comment record is stored in the inject.js-compatible format:
        {"_s": "web_api", "ts": <ms>, "d": {"feedId": ..., "totalNum": ..., "vecComment": [...]}}

    Deduplication is delegated to ``Store.append_comment`` which computes a
    key from sorted comment IDs (matching inject.js:145-157).
    """

    def __init__(
        self,
        client,
        store,
        guild_number: str,
        max_workers: int = 10,
        shared_semaphore: threading.Semaphore | None = None,
        media_downloader=None,
    ):
        """Initialize the comments scraper.

        Args:
            client: QQWebClient instance (provides get_feed_comments()).
            store: Store instance (provides append_comment() for dedup/writes).
            guild_number: Channel guild_number slug (e.g. "Takagi3channel").
                Carried for completeness; the client itself embeds it in the
                channelSign body when calling GetFeedComments.
            max_workers: Default ThreadPoolExecutor size for scrape_all().
            shared_semaphore: Optional ``threading.Semaphore`` used to bound
                GLOBAL API concurrency across all guild contexts (plan §2.1a / I1).
                When provided, the semaphore is acquired ONLY around the
                ``client.get_feed_comments`` network call — not during store
                writes — so bookkeeping remains parallel. ``None`` (default)
                preserves single-guild behavior unchanged.
            media_downloader: Optional :class:`MediaDownloader`. When provided,
                each newly-fetched comment page has its ``richContents`` images
                + stickers downloaded and indexed. ``None`` (default) preserves
                the pre-comment-media behaviour exactly.
        """
        self.client = client
        self.store = store
        self.guild_number = guild_number
        self.max_workers = max_workers
        self._semaphore = shared_semaphore
        self._media_downloader = media_downloader
        self._log = logging.getLogger(__name__)

    def _sem_ctx(self):
        if self._semaphore is None:
            return contextlib.nullcontext()
        return self._semaphore

    def scrape_feed_comments(self, feed_id: str, total_hint: int | None = None) -> int:
        """Paginate through comments for a single feed.

        Args:
            feed_id: Target feed ID (e.g. "B_123").
            total_hint: Optional expected totalNum (currently informational
                only; pagination is driven by attchInfo cursors).

        Returns:
            Number of NEW comment pages written (post-dedup).
        """
        attch_info = ""
        total_new = 0
        pages = 0

        while pages < _MAX_PAGES_PER_FEED:
            pages += 1
            try:
                with self._sem_ctx():
                    vec_comment, total_num, attch_info = self.client.get_feed_comments(
                        feed_id, attch_info=attch_info
                    )
            except Exception:
                self._log.exception("get_feed_comments failed for feed=%s page=%d", feed_id, pages)
                break

            if not vec_comment:
                break

            record: dict[str, Any] = {
                "_s": "web_api",
                "ts": int(time.time() * 1000),  # milliseconds, matching inject.js Date.now()
                "d": {
                    "feedId": feed_id,
                    "totalNum": total_num,
                    "vecComment": vec_comment,
                },
            }

            try:
                is_new = self.store.append_comment(record)
            except Exception:
                self._log.exception("store.append_comment failed for feed=%s", feed_id)
                break

            if self._media_downloader:
                img_count = 0
                for comment in vec_comment:
                    try:
                        img_count += self._media_downloader.download_comment_media(
                            comment, feed_id=feed_id
                        )
                    except Exception:
                        self._log.exception(
                            "comment media download failed for feed=%s", feed_id
                        )
                if img_count > 0:
                    self._log.info(
                        "comment media: feed %s — %d image(s) across %d comment(s)",
                        feed_id, img_count, len(vec_comment),
                    )

            if is_new:
                total_new += 1

            if not attch_info:
                break

        if pages >= _MAX_PAGES_PER_FEED and attch_info:
            self._log.warning(
                "scrape_feed_comments hit safety limit (%d pages) for feed=%s; stopping",
                _MAX_PAGES_PER_FEED,
                feed_id,
            )

        return total_new

    def scrape_all(self, feeds: Iterable[Any], max_workers: int | None = None) -> int:
        """Scrape comments for many feeds concurrently.

        Args:
            feeds: Iterable of either feed dicts (``{"id":..., "commentCount":N}``)
                or ``(feed_id, comment_count)`` tuples. Feeds with
                ``commentCount == 0`` are skipped (no API call).
            max_workers: Override ``self.max_workers`` for this call.

        Returns:
            Total new comment pages across all feeds.
        """
        targets: list[str] = []
        skipped_zero = 0
        for feed in feeds:
            feed_id, count = _extract_feed_id_and_count(feed)
            if feed_id is None:
                continue
            if not count:
                skipped_zero += 1
                continue
            targets.append(feed_id)

        if skipped_zero:
            self._log.debug("Skipping %d feeds with 0 comments", skipped_zero)

        if not targets:
            self._log.info("No feeds with comments to scrape")
            return 0

        workers = max_workers or self.max_workers
        total_new = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.scrape_feed_comments, fid): fid for fid in targets}
            for fut in as_completed(futures):
                fid = futures[fut]
                try:
                    total_new += fut.result()
                except Exception:
                    self._log.exception(
                        "scrape_feed_comments raised for feed=%s", fid
                    )

        self._log.info(
            "Scraped comments for %d feeds, %d new comments", len(targets), total_new
        )
        return total_new


def _extract_feed_id_and_count(feed: Any) -> tuple[str | None, int]:
    """Extract (feed_id, comment_count) from a feed dict OR a tuple.

    Args:
        feed: Either a dict with ``id``/``commentCount`` keys, or a
            ``(feed_id, comment_count)`` tuple/list.

    Returns:
        ``(feed_id, comment_count)`` — feed_id may be None if unparseable,
        comment_count defaults to 0.
    """
    if isinstance(feed, dict):
        feed_id = feed.get("id")
        if feed_id is None:
            feed_id = feed.get("feedId")
        try:
            count = int(feed.get("commentCount", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        return (feed_id, count)

    if isinstance(feed, (tuple, list)):
        if len(feed) >= 1:
            feed_id = feed[0]
            count = 0
            if len(feed) >= 2:
                try:
                    count = int(feed[1] or 0)
                except (TypeError, ValueError):
                    count = 0
            return (feed_id, count)

    return (None, 0)
