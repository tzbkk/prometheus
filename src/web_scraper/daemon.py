"""Daemon loop：实体树扫描周期 + 无记忆增长检测。

周期结构（每 guild 顺序执行）：

1. 列表观测：GetGuildFeeds + 逐频道 GetChannelTimelineFeeds →
   save_feed（观测即重写：顶层 verbatim 刷新 + last_seen）+ 媒体投递
   （池化——enqueue 零阻塞，下载在后台池推进）；
   同时现拿 API commentCount（增长检测 API 侧，无记忆比较）。
2. 增长重拉：growth_targets(api_counts, store.local_comment_counts())
   → 逐帖 scrape_feed_comments（新评论落实体，索引写侧增量）。
3. 老帖轮换 recheck：窗口外老帖不在列表 → 无 API 计数可比；轮换
   批直接重拉评论（noauth 按 feed_id 直取——"老帖新评论"通道），
   create-only 语义下已存实体零重写。
4. 进程锁 touch（entity_store.lock，ProcessLock 5 字段契约含 bottomReached）。

网关拒绝可见性：client 计数 code!=0（gateway_rejects），周期末按 guild 升
ERROR 并入 stats（gateway_rejects 键）——noauth 被锁时"空页≠没有新帖"不再
静默（僵尸绿灯防线）。

trigger 幂等（ScraperAction 契约）：run_once_guarded 非阻塞抢锁，周期
已在跑则受理但跳过执行（"守护已在跑亦返回 true"的服务侧代价为零）。
"""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import signal
import threading
import time
from typing import Any

from src.entity_store.lock import ProcessLockData, write_lock
from src.web_scraper.store import SKIPPED, growth_targets

logger = logging.getLogger(__name__)

_MAX_CHANNEL_PAGES = 500


class Daemon:
    """Periodic rescanner driving feeds/comments/media over entity contexts."""

    def __init__(
        self,
        guild_contexts: list,
        interval_sec: float = 120,
        stats: dict[str, Any] | None = None,
        lock_path=None,
        now_ms=None,
    ):
        """Args:
            guild_contexts: GuildContext 列表（__main__ 组装：guild/client/
                store/feeds_scraper/comments_scraper/media_downloader +
                recheck 轮换字段 + bottom_reached）。
            interval_sec: 周期间隔秒。
            stats: 与 API 服务共享的快照 dict。
            lock_path: data_root/prometheus.lock（周期 touch 用）。
            now_ms: 时钟缝（测试注入）。
        """
        self.guild_contexts = guild_contexts
        self.interval_sec = interval_sec
        self.stats: dict[str, Any] = stats if stats is not None else {}
        self.stats.setdefault("scanned_feeds", 0)
        self.stats.setdefault("feeds", 0)
        self.stats.setdefault("comments", 0)
        self.stats.setdefault("replies", 0)
        self.stats.setdefault("media", 0)
        self.stats.setdefault("last_scan_ts", 0)
        self.stats.setdefault("daemon_running", False)
        self.stats.setdefault("guilds", {})
        self._lock_path = lock_path
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._stop_event = threading.Event()
        self._run_guard = threading.Lock()
        self._cycle = 0
        self._log = logging.getLogger(__name__)

    @staticmethod
    def _release_memory():
        """Force Python to return freed heap pages to the OS."""
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (OSError, AttributeError):
            pass

    def _touch_lock(self, *, dirty: bool) -> None:
        if self._lock_path is None:
            return
        write_lock(
            self._lock_path,
            ProcessLockData(
                pid=os.getpid(),
                dirty=dirty,
                cycle=self._cycle,
                ts=int(time.time()),
                bottomReached=any(
                    getattr(ctx, "bottom_reached", False) for ctx in self.guild_contexts
                ),
            ),
        )

    def run_once_guarded(self) -> bool:
        """trigger 入口（幂等受理）：周期已在跑 → 受理但不再入（返回 False）。"""
        if not self._run_guard.acquire(blocking=False):
            self._log.info("cycle already running — trigger absorbed (idempotent)")
            return False
        try:
            self.run_once()
            return True
        finally:
            self._run_guard.release()

    def run_once(self) -> dict[str, Any]:
        """Execute one scan cycle across ALL guild contexts（逐 guild 容错）."""
        self.stats["daemon_running"] = True
        self._cycle += 1
        try:
            self._touch_lock(dirty=True)
            for ctx in self.guild_contexts:
                try:
                    self._scan_guild(ctx)
                except Exception:
                    self._log.exception("scan failed for guild %s", ctx.guild.guild_id)
            self._aggregate_stats()
        finally:
            self.stats["daemon_running"] = False
            self._release_memory()
        return self.stats

    def _observe_listing_page(self, ctx, vec_feed, api_counts: dict[str, int]) -> int:
        """单页观测：save_feed + 媒体投递（池化异步，零阻塞）+ API 侧计数现拿。

        返回新 feed 数。媒体下载不在此路径等待（288+ 媒体逐个内联
        会把首跑列表拖成 20+ 分钟零评论；投递后评论阶段紧随列表完成）。
        """
        page_new = 0
        for feed in vec_feed:
            if not ctx.feeds_scraper._accepts(feed):
                continue
            fid = feed.get("id", "")
            try:
                cc = int(feed.get("commentCount", 0) or 0)
            except (TypeError, ValueError):
                cc = 0
            status = ctx.store.save_feed(feed)
            if status != SKIPPED:
                api_counts[fid] = cc
                self.stats["scanned_feeds"] = int(self.stats.get("scanned_feeds", 0)) + 1
            if status == "created":
                page_new += 1
            try:
                ctx.media_downloader.attempt_entity_media(fid)
            except Exception:
                self._log.exception("media enqueue failed for feed=%s", fid)
        return page_new

    def _scan_guild(self, ctx) -> None:
        """单 guild 周期：列表观测 → 增长重拉 → recheck 轮换。"""
        api_counts: dict[str, int] = {}
        rejects_before = getattr(ctx.client, "gateway_rejects", 0)

        feeds_attch = ""
        while True:
            try:
                vec_feed, feeds_attch, is_finish = ctx.client.get_feeds(0, feeds_attch)
            except Exception:
                self._log.exception("GetGuildFeeds failed")
                break
            if not vec_feed:
                ctx.bottom_reached = True
                break
            page_new = self._observe_listing_page(ctx, vec_feed, api_counts)
            if is_finish:
                ctx.bottom_reached = True
            if page_new == 0 or is_finish or not feeds_attch:
                break

        channels = ctx.client.get_guild_channels()
        if not channels:
            channels = [{"channel_id": ctx.feeds_scraper.channel_id, "name": "default"}]

        for ch in channels:
            ch_id = str(ch.get("channel_id", ""))
            if not ch_id:
                continue
            ch_attch = ""
            for _ in range(_MAX_CHANNEL_PAGES):
                try:
                    vec_feed, ch_attch, finish = ctx.client.get_channel_feeds(
                        ch_id, 0, ch_attch
                    )
                except Exception:
                    self._log.exception("channel feed fetch failed for %s", ch_id)
                    break
                if not vec_feed:
                    break
                page_new = self._observe_listing_page(ctx, vec_feed, api_counts)
                if page_new == 0 or finish or not ch_attch:
                    break

        targets = growth_targets(api_counts, ctx.store.local_comment_counts())
        if targets:
            self._log.info("growth detection: %d feed(s) to re-pull", len(targets))
        ctx.comments_scraper.scrape_all(
            [(fid, api_counts[fid]) for fid in sorted(targets)],
            max_workers=ctx._recheck_workers,
        )

        old_ids = ctx.store.feed_ids_with_comments()
        if old_ids:
            n = len(old_ids)
            batch = min(ctx._recheck_batch_size, n)
            start = ctx._recheck_cursor % n
            batch_ids = [old_ids[(start + i) % n] for i in range(batch)]
            ctx._recheck_cursor = (start + batch) % n
            try:
                ctx.comments_scraper.scrape_all(
                    [(fid, 1) for fid in batch_ids], max_workers=ctx._recheck_workers
                )
            except Exception:
                self._log.exception("old feed comment re-check failed")

        gateway_rejects = getattr(ctx.client, "gateway_rejects", 0) - rejects_before
        if gateway_rejects:
            self._log.error(
                "gateway rejected %d API call(s) this cycle for guild %s — "
                "empty pages may be a lock, not 'no new posts'",
                gateway_rejects,
                ctx.guild.guild_id,
            )

        self.stats.setdefault("guilds", {})[ctx.guild.guild_id] = {
            "feeds": ctx.store.created_feeds,
            "comments": ctx.store.created_comments,
            "replies": ctx.store.created_replies,
            "media": ctx.media_downloader.downloaded_count,
            "last_scan_ts": int(time.time()),
            "gateway_rejects": gateway_rejects,
        }

    def _aggregate_stats(self) -> None:
        """Sum per-guild cumulative counters into top-level totals."""
        guilds = self.stats.get("guilds", {})
        for key in ("feeds", "comments", "replies", "media"):
            self.stats[key] = sum(g.get(key, 0) for g in guilds.values())
        self.stats["gateway_rejects"] = sum(
            g.get("gateway_rejects", 0) for g in guilds.values()
        )
        self.stats["last_scan_ts"] = max(
            (g.get("last_scan_ts", 0) for g in guilds.values()),
            default=int(time.time()),
        )

    def run_forever(self) -> None:
        """Run :meth:`run_once` every ``interval_sec`` until :meth:`stop`."""
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

    def wait_idle(self, timeout: float | None = None) -> bool:
        """等待在途周期（trigger 线程）结束；超时 False。

        停机序列用（SIGTERM → run_forever 退出后、媒体池 drain 前调用）——
        确保半途 trigger 周期的投递全部落池后再关门。
        """
        if self._run_guard.acquire(timeout=timeout):
            self._run_guard.release()
            return True
        return False

    def _install_signal_handlers(self) -> None:
        """Wire SIGTERM/SIGINT to :meth:`_on_signal`."""
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        except (ValueError, OSError):
            self._log.debug(
                "signal handlers not installed (not main thread); use stop() to terminate"
            )

    def _on_signal(self, signum, frame) -> None:
        self._log.info("Received signal %d, stopping daemon", signum)
        self._stop_event.set()
