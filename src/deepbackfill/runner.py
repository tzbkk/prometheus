"""全史回填编排（数据面五键全同——组件全部复用 web_scraper 现成件）。

新代码只出现在凭据面+获取器+编排：本模块即编排面。
复用缝（池化生产形态）：

- EntityStore（entity_store 写者门面——save_feed/save_comment/增长索引）；
- CommentsScraper + noauth QQWebClient（评论补齐，无时间窗——§0 修正：
  深捕一次运行自含评论/回复全管线，不依赖事后慢补）；
- MediaDownloadPool + MediaDownloader（池化——attempt_entity_media
  投递零阻塞，列表观测不被媒体下载挡路）；
- growth_targets（无记忆比较：API commentCount vs 本地评论文件数）。

周期结构（单次 run，§2.3 翻页语义）：

1. auth 全史翻页：cursor="" 起、data.feedAttchInfo 回填；逐帖 save_feed
   （观测即重写）+ 媒体投递 + API 侧 commentCount 现拿；
   isFinish==true 干净终止；游标不变/空 = 异常终止（warning，不 raise——
   已落盘数据保真）。
2. 增长检测补齐：api_cc > 本地 → noauth scrape_feed_comments
   （新评论落实体 + 媒体投递）。

进度计数线程安全：stats dict 一切更新经 _stat_lock（/health /stats 读侧
零撕裂）；实体计数取 store 累计器（自带锁）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from src.web_scraper.store import SKIPPED, growth_targets

__all__ = ["BackfillRunner"]

logger = logging.getLogger(__name__)


class BackfillRunner:
    """一次性全史回填编排器（trigger 触发后台线程；运行中再触发 = busy）。"""

    def __init__(
        self,
        guild_contexts: list,
        *,
        stats: dict[str, Any] | None = None,
        stop_event: threading.Event | None = None,
    ):
        """Args:
            guild_contexts: 每 guild 组装件（auth_client/store/
                comments_scraper/media_downloader/guild——__main__ 组装，
                tests/web_scraper/conftest.build_guild_context 同形惯例）。
            stats: 与 API 服务共享的进度 dict（缺省自建）。
            stop_event: 优雅停机缝（SIGTERM 序列置位——页间检查点退出）。
        """
        self.guild_contexts = guild_contexts
        self.stats: dict[str, Any] = stats if stats is not None else {}
        for key, default in (
            ("scanned_feeds", 0),
            ("pages", 0),
            ("feeds", 0),
            ("comments", 0),
            ("replies", 0),
            ("media", 0),
            ("running", False),
            ("guilds", {}),
        ):
            self.stats.setdefault(key, default)
        self._stop_event = stop_event or threading.Event()
        self._stat_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 观测面
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self.stats.get("running"))

    def live_stats(self) -> dict[str, Any]:
        """/stats 进度快照：翻页/扫描计数取 stats dict（同步单调），四类实体
        计数从 store/downloader 累计器现算——媒体经后台池异步推进，
        dict 快照会滞后，live 读数才反映真实进度。
        """
        with self._stat_lock:
            view = dict(self.stats)
        view["feeds"] = sum(ctx.store.created_feeds for ctx in self.guild_contexts)
        view["comments"] = sum(ctx.store.created_comments for ctx in self.guild_contexts)
        view["replies"] = sum(ctx.store.created_replies for ctx in self.guild_contexts)
        view["media"] = sum(
            ctx.media_downloader.downloaded_count for ctx in self.guild_contexts
        )
        return view

    def _bump(self, key: str, delta: int = 1) -> None:
        with self._stat_lock:
            self.stats[key] = int(self.stats.get(key, 0)) + delta

    # ------------------------------------------------------------------
    # 线程面（trigger 消费）
    # ------------------------------------------------------------------
    def start_background(self) -> bool:
        """起后台回填线程；已在跑 → False（409 busy 的判定源）。"""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(
                target=self._run_guarded, name="deepbackfill-run", daemon=True
            )
            self._thread.start()
            return True

    def join(self, timeout: float | None = None) -> bool:
        """等待回填线程结束（停机序列：service.stop 后、媒体池 drain 前）。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            return not thread.is_alive()
        return True

    def request_stop(self) -> None:
        self._stop_event.set()

    def _run_guarded(self) -> None:
        try:
            self.run()
        except Exception:
            logger.exception("deepbackfill run crashed")

    # ------------------------------------------------------------------
    # 编排面
    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        """同步执行全史回填（后台线程体；单 guild 容错）。"""
        with self._stat_lock:
            self.stats["running"] = True
        try:
            for ctx in self.guild_contexts:
                if self._stop_event.is_set():
                    logger.info("backfill stop requested — halting before guild %s",
                                getattr(ctx.guild, "guild_id", "?"))
                    break
                try:
                    self._backfill_guild(ctx)
                except Exception:
                    logger.exception(
                        "backfill failed for guild %s",
                        getattr(ctx.guild, "guild_id", "?"),
                    )
            self._aggregate()
        finally:
            with self._stat_lock:
                self.stats["running"] = False
        return self.stats

    def _backfill_guild(self, ctx) -> None:
        guild_id = ctx.guild.guild_id
        api_counts: dict[str, int] = {}
        cursor = ""
        pages = 0

        while not self._stop_event.is_set():
            vec_feed, attach, finish = ctx.auth_client.get_feeds(cursor)
            pages += 1
            self._bump("pages")

            for feed in vec_feed:
                self._ingest_feed(ctx, feed, api_counts)

            if finish:
                logger.info(
                    "guild %s: default stream exhausted after %d page(s) (isFinish=true)",
                    guild_id, pages,
                )
                break
            if not attach or attach == cursor:
                logger.warning(
                    "guild %s: pagination stalled at page %d (cursor %r) — "
                    "stopping with on-disk data intact",
                    guild_id, pages, attach[:32],
                )
                break
            cursor = attach

        pages += self._walk_channels(ctx, api_counts)

        targets = growth_targets(api_counts, ctx.store.local_comment_counts())
        if targets:
            logger.info(
                "guild %s: growth detection — re-pulling comments for %d feed(s)",
                guild_id, len(targets),
            )
        for fid in sorted(targets):
            if self._stop_event.is_set():
                break
            try:
                ctx.comments_scraper.scrape_feed_comments(fid)
            except Exception:
                logger.exception("growth comment scrape failed for feed=%s", fid)

        with self._stat_lock:
            self.stats.setdefault("guilds", {})[guild_id] = {
                "pages": pages,
                "feeds": ctx.store.created_feeds,
                "comments": ctx.store.created_comments,
                "replies": ctx.store.created_replies,
                "media": ctx.media_downloader.downloaded_count,
            }

    def _ingest_feed(self, ctx, feed, api_counts: dict[str, int]) -> None:
        if not isinstance(feed, dict):
            return
        fid = feed.get("id", "")
        try:
            cc = int(feed.get("commentCount", 0) or 0)
        except (TypeError, ValueError):
            cc = 0
        status = ctx.store.save_feed(feed)
        if status != SKIPPED:
            api_counts[fid] = cc
            self._bump("scanned_feeds")
        try:
            ctx.media_downloader.attempt_entity_media(fid)
        except Exception:
            logger.exception("media enqueue failed for feed=%s", fid)

    def _walk_channels(self, ctx, api_counts: dict[str, int]) -> int:
        """逐频道 timeline 全量走——默认流是混合流（广场为主 + 各频道
        少量热帖），只覆盖每频道一小部分；全量必须逐频道走 timeline
        （save 按 feed id 幂等去重，重复走无害）。"""
        guild_id = ctx.guild.guild_id
        channels: list = []
        try:
            channels = ctx.auth_client.get_guild_channels()
        except Exception:
            logger.exception(
                "guild %s: channel list unavailable — continuing with "
                "default-stream feeds only",
                guild_id,
            )
            return 0
        if not channels:
            try:
                channels = ctx.auth_client.get_guild_channels()
            except Exception:
                logger.exception(
                    "guild %s: channel list retry failed — continuing "
                    "stream-only",
                    guild_id,
                )
                return 0
        if not channels:
            logger.warning(
                "guild %s: channel list empty after retry — continuing "
                "stream-only",
                guild_id,
            )
            return 0
        logger.info(
            "guild %s: channel list %d entr(ies) — walking timelines",
            guild_id, len(channels),
        )
        pages = 0
        for ch in channels:
            if self._stop_event.is_set():
                break
            cid = str(ch.get("channel_id", "") or "")
            if not cid:
                continue
            name = ch.get("name", "")
            ch_pages, ch_new = self._walk_one_channel(ctx, cid, api_counts)
            pages += ch_pages
            logger.info(
                "guild %s channel %s (%s): %d page(s), %d new feed(s)",
                guild_id, cid, name, ch_pages, ch_new,
            )
        return pages

    def _walk_one_channel(
        self, ctx, channel_id: str, api_counts: dict[str, int]
    ) -> tuple[int, int]:
        guild_id = ctx.guild.guild_id
        cursor = ""
        pages = 0
        new_feeds = 0
        while not self._stop_event.is_set():
            try:
                vec_feed, attach, finish = ctx.auth_client.get_channel_feeds(
                    channel_id, cursor
                )
            except Exception:
                logger.exception(
                    "guild %s channel %s: timeline fetch failed — "
                    "stopping this channel with on-disk data intact",
                    guild_id, channel_id,
                )
                break
            pages += 1
            self._bump("pages")
            for feed in vec_feed:
                if not isinstance(feed, dict):
                    continue
                fid = feed.get("id", "")
                status = ctx.store.save_feed(feed)
                if status != SKIPPED:
                    new_feeds += 1
                    self._bump("scanned_feeds")
                    try:
                        cc = int(feed.get("commentCount", 0) or 0)
                    except (TypeError, ValueError):
                        cc = 0
                    api_counts[fid] = cc
                try:
                    ctx.media_downloader.attempt_entity_media(fid)
                except Exception:
                    logger.exception("media enqueue failed for feed=%s", fid)
            if finish or not vec_feed:
                break
            if not attach or attach == cursor:
                logger.warning(
                    "guild %s channel %s: pagination stalled at page %d",
                    guild_id, channel_id, pages,
                )
                break
            cursor = attach
        return pages, new_feeds

    def _aggregate(self) -> None:
        guilds = self.stats.get("guilds", {})
        with self._stat_lock:
            self.stats["pages"] = sum(g.get("pages", 0) for g in guilds.values())
            for key in ("feeds", "comments", "replies", "media"):
                self.stats[key] = sum(g.get(key, 0) for g in guilds.values())
