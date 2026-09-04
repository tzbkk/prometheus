"""评论管线 mandate：growth 重拉并行 + 轮换批抓取。

growth 相位走 scrape_all（并发受 _recheck_workers=8），轮换批 150——
评论帖全轮换一圈 4 周期；HTTP 并发天花板 = 全局 Semaphore(10) 礼貌护栏。
"""

from __future__ import annotations

import logging

from tests.web_scraper.conftest import (
    COMMENT_1,
    COMMENT_2,
    FEED_A,
    FEED_B,
    FakeClient,
    build_guild_context,
    synthetic_comment,
    synthetic_feed,
)


def test_daemon_growth_phase_scrapes_all_targets(tmp_path, caplog):
    """MD-136：growth 相位经 scrape_all 全量落盘——多帖增长目标并行抓评论，
    每帖评论全数落实体（并行路径不丢目标、不依赖串行次序）。"""
    feeds = [
        synthetic_feed(FEED_A, comment_count=1),
        synthetic_feed(FEED_B, comment_count=2),
    ]
    comments = {
        FEED_A: [synthetic_comment(COMMENT_1)],
        FEED_B: [synthetic_comment(COMMENT_2)],
    }
    harness = build_guild_context(tmp_path, FakeClient(feeds, comments))
    with caplog.at_level(logging.INFO, logger="src.web_scraper.comments"):
        harness.daemon.run_once()

    assert harness.store.created_comments == 2
    scraped_batches = [
        r.getMessage() for r in caplog.records if "Scraped comments for" in r.getMessage()
    ]
    assert scraped_batches, "growth phase must flow through scrape_all batch logging"
