"""Comments scraper：评论/回复 → c_/r_ 实体。

评论元素逐个
落实体：顶层 verbatim（含 vecReply 内嵌克隆，双存）+ _p（feed_id
页上下文注入）；回复另立 r_ 实体。去重 = 文件存在性（create-only）。

HTTP 面复用：QQWebClient.get_feed_comments 翻页 + 全局并发信号量
（信号量只围网络调用，不围实体写）。
"""

from __future__ import annotations

import contextlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

from src.web_scraper.store import CREATED

logger = logging.getLogger(__name__)

# Safety limit: prevent infinite pagination loops per feed.
_MAX_PAGES_PER_FEED = 50


class CommentsScraper:
    """Scrapes comments for feeds; persists comment/reply entities + media."""

    def __init__(
        self,
        client,
        store,
        max_workers: int = 10,
        shared_semaphore: threading.Semaphore | None = None,
        media_downloader=None,
    ):
        """Args:
            client: QQWebClient（get_feed_comments）.
            store: EntityStore（save_comment + 增长索引）.
            max_workers: scrape_all() 的 ThreadPool 大小.
            shared_semaphore: 全局 API 并发上界（多 guild 共享）.
            media_downloader: MediaDownloader——每元素落盘后投递其
                pending/failed 媒体（池化异步；跨周期 retries 累积）。
        """
        self.client = client
        self.store = store
        self.max_workers = max_workers
        self._semaphore = shared_semaphore
        self._media_downloader = media_downloader
        self._log = logging.getLogger(__name__)

    def _sem_ctx(self):
        if self._semaphore is None:
            return contextlib.nullcontext()
        return self._semaphore

    def scrape_feed_comments(self, feed_id: str) -> int:
        """Paginate comments for one feed; return NEW entity count (c_ + r_).

        每元素：save_comment（评论本体）→ 媒体尝试 → vecReply 逐回复
        save_comment + 媒体尝试。畸形元素 skip+log（store 边界），单元素
        异常不炸整帖翻页。
        """
        attch_info = ""
        total_new = 0
        pages = 0

        while pages < _MAX_PAGES_PER_FEED:
            pages += 1
            try:
                with self._sem_ctx():
                    vec_comment, _total_num, attch_info = self.client.get_feed_comments(
                        feed_id, attch_info=attch_info
                    )
            except Exception:
                self._log.exception("get_feed_comments failed for feed=%s page=%d", feed_id, pages)
                break

            if not vec_comment:
                break

            for node in vec_comment:
                total_new += self._persist_one(node, feed_id)
                reply_list = node.get("vecReply") if isinstance(node, dict) else None
                for reply in reply_list or []:
                    if isinstance(reply, dict):
                        total_new += self._persist_one(reply, feed_id)

            if not attch_info:
                break

        if pages >= _MAX_PAGES_PER_FEED and attch_info:
            self._log.warning(
                "scrape_feed_comments hit safety limit (%d pages) for feed=%s; stopping",
                _MAX_PAGES_PER_FEED,
                feed_id,
            )

        return total_new

    def _persist_one(self, node: dict, feed_id: str) -> int:
        """单评论/回复元素 → 实体 + 媒体投递（池化异步）；返回 1 若新落盘。"""
        status = self.store.save_comment(node, feed_id=feed_id)
        node_id = node.get("id")
        if status == "skipped" or not isinstance(node_id, str):
            return 0
        if self._media_downloader is not None:
            try:
                self._media_downloader.attempt_entity_media(node_id)
            except Exception:
                self._log.exception("media enqueue failed for entity=%s", node_id)
        return 1 if status == CREATED else 0

    def scrape_all(self, feeds: Iterable[Any], max_workers: int | None = None) -> int:
        """Scrape comments for many feeds concurrently（recheck 轮换面）.

        Args:
            feeds: (feed_id, comment_count) 元组或 feed dict；count==0 跳过。
            max_workers: 覆盖 self.max_workers。
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
            "Scraped comments for %d feeds, %d new entities", len(targets), total_new
        )
        return total_new


def _extract_feed_id_and_count(feed: Any) -> tuple[str | None, int]:
    """Extract (feed_id, comment_count) from a feed dict OR a tuple."""
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
