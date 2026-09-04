"""派生索引启动扫描 mandate 测试（MD-027 ~ MD-030，pillar 对应面+C）。

契约溯源：（增长检测终案——一机制两用：per-feed comment/reply 计数 + dead-URL 集）
+ §十一（归档窗 = createTime ∈ (from,to] 业务时钟，三类实体各自判窗，
与 _p.last_seen 观测时钟之分）+ 原则 3（派生态纯内存零落盘）。
合法树经 write_entity 生成（合成树惯例——合法形态由写者保证）；畸形文件手搓直写
（宽容遍历负例面——边界严格性归 load 侧的语义对照）。
"""

from __future__ import annotations

import json
import logging

import pytest

from src.entity_store import (
    ScanResult,
    comment_path,
    iter_window,
    scan,
    shard_of,
    write_entity,
)

GUILD = "1000000000000001"
FEED_A = "B_9d8c7b6a5f4e3d2c1b0a9988776655443322110f"
FEED_B = "B_0f9e8d7c6b5a4938271605f4e3d2c1b0a998877a"
C1 = "c_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"
C2 = "c_2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a01"
C3 = "c_3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0123"
C4 = "c_4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a012345"
R1 = "r_0f9e8d7c6b5a4938271605f4e3d2c1b0a99887"
R2 = "r_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9b"

T1 = 1782919600000

URL_PENDING = "https://channel.photo.store.qq.com/pending.png"
URL_DEAD_FEED = "https://channel.photo.store.qq.com/deed-feed.png"
URL_FAILED = "https://channel.photo.store.qq.com/failed.png"
URL_DEAD_COMMENT = "https://channel.photo.store.qq.com/dead-comment.png"
URL_DEAD_GHOST = "https://channel.photo.store.qq.com/ghost.png"


def media_entry(url: str, **overrides) -> dict:
    """8 字段块全钉形态（，与 test_writer 同款）。"""
    entry = {
        "url": url,
        "file": None,
        "type": "image",
        "width": None,
        "height": None,
        "status": "pending",
        "retries": 0,
        "last_attempt_ts": None,
    }
    entry.update(overrides)
    return entry


def feed_body(feed_id: str, **overrides) -> dict:
    body = {
        "id": feed_id,
        "createTime": "1782919600",
        "title": "合成中文帖子",
        "channelInfo": {"sign": {"guild_id": GUILD}},
        "postUser": {"id": "u_1", "nick": "作者"},
    }
    body.update(overrides)
    return body


def comment_body(comment_id: str, **overrides) -> dict:
    body = {
        "id": comment_id,
        "content": "合成中文评论",
        "createTime": "1782920526",
        "postUser": {"id": "u_2", "nick": "评论者"},
    }
    body.update(overrides)
    return body


def write_feed(data_root, feed_id, media=(), create_time="1782919600"):
    return write_entity(
        data_root,
        GUILD,
        feed_id,
        feed_body(feed_id, createTime=create_time),
        captured_via="scraper",
        media=list(media),
        now_ms=T1,
    )


def write_comment(
    data_root, comment_id, feed_id, media=(), create_time="1782920526", now_ms=T1
):
    return write_entity(
        data_root,
        GUILD,
        comment_id,
        comment_body(comment_id, createTime=create_time),
        captured_via="scraper",
        media=list(media),
        now_ms=now_ms,
        feed_id=feed_id,
    )


def write_raw_comment(data_root, comment_id: str, doc: dict):
    """手搓直写（畸形面专用）：合法分片路径 + 任意内容（绕过 writer 纪律）。"""
    path = comment_path(data_root, GUILD, comment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def minimal_p(feed_id: str | None = None, media=None) -> dict:
    """_p 最小合法块（畸形注入的基座；feed_id=None 即缺 feed_id 面）。"""
    p: dict = {"captured_via": "scraper", "first_seen": 1, "last_seen": 2}
    if feed_id is not None:
        p = {"feed_id": feed_id, **p}
    p["media"] = [] if media is None else media
    return p


def test_scan_counts_per_feed_and_collects_dead_urls(data_root):
    write_feed(
        data_root,
        FEED_A,
        media=[
            media_entry(URL_PENDING),
            media_entry(URL_DEAD_FEED, status="dead", retries=3),
            media_entry(URL_FAILED, status="failed", retries=2),
        ],
    )
    write_feed(data_root, FEED_B)
    write_comment(data_root, C1, FEED_A, media=[media_entry(URL_DEAD_COMMENT, status="dead")])
    write_comment(data_root, C2, FEED_A)
    write_comment(data_root, C3, FEED_B)
    write_comment(data_root, R1, FEED_A)

    before = sorted(p for p in data_root.rglob("*"))

    result = scan(data_root, GUILD)

    assert sorted(p for p in data_root.rglob("*")) == before  # 纯内存——零落盘（原则 3）
    assert result.comment_counts == {FEED_A: 2, FEED_B: 1}
    assert result.reply_counts == {FEED_A: 1}
    assert result.dead_urls == {URL_DEAD_FEED, URL_DEAD_COMMENT}  # pending/failed 不入
    assert result.feeds == 2
    assert result.skipped == 0


def test_scan_skips_malformed_entities_with_warning(data_root, caplog):
    feed_path_written = write_feed(
        data_root, FEED_A, media=[media_entry(URL_DEAD_FEED, status="dead")]
    )
    write_comment(data_root, C1, FEED_A)

    corrupt = comment_path(data_root, GUILD, C2)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{ not json", encoding="utf-8")

    no_p = write_raw_comment(data_root, C3, {"id": C3, "createTime": "1782920526"})

    wrong_shard = next(s for s in (f"{i:02x}" for i in range(256)) if s != shard_of(C4))
    misplaced = data_root / GUILD / "comments" / f"c_{wrong_shard}" / f"{C4}.json"
    misplaced.parent.mkdir(parents=True, exist_ok=True)
    misplaced.write_text(
        json.dumps({"id": C4, "createTime": "1782920526", "_p": minimal_p(FEED_A)}),
        encoding="utf-8",
    )

    no_feed_id = write_raw_comment(
        data_root,
        R1,
        {"id": R1, "createTime": "1782920526", "_p": minimal_p(None)},
    )

    bad_media = write_raw_comment(
        data_root,
        R2,
        {"id": R2, "createTime": "1782920526", "_p": minimal_p(FEED_A, media={})},
    )

    entry_no_url = write_raw_comment(
        data_root,
        "c_5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0123456",
        {
            "id": "c_5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0123456",
            "createTime": "1782920526",
            "_p": minimal_p(FEED_A, media=[{"status": "dead"}]),  # 缺 url——死状态不入账
        },
    )

    caplog.set_level(logging.WARNING, logger="src.entity_store.scan")
    result = scan(data_root, GUILD)

    assert result.comment_counts == {FEED_A: 1}
    assert result.reply_counts == {}
    assert result.feeds == 1
    assert result.skipped == 6
    assert result.dead_urls == {URL_DEAD_FEED}  # 幽灵 dead（缺 url 条目）不泄漏
    assert len(caplog.records) == 6
    for malformed in (corrupt, no_p, misplaced, no_feed_id, bad_media, entry_no_url):
        assert str(malformed) in caplog.text
    assert str(feed_path_written) not in caplog.text


def test_iter_window_half_open_on_create_time(data_root, caplog):
    # 三类实体各自按自身 createTime 判窗；观测时钟 _p.last_seen 不参与判窗
    write_feed(data_root, FEED_A, create_time="1000")  # == from → 排除（半开）
    write_feed(data_root, FEED_B, create_time="2500")  # 窗外
    write_comment(  # createTime < from → 排除，尽管 last_seen 折秒 1500 落窗内（反证）
        data_root, C1, FEED_A, create_time="999", now_ms=1_500_000
    )
    write_comment(data_root, C2, FEED_A, create_time="1000")  # == from → 排除（半开逐字）
    write_comment(  # createTime 窗内 → 含入，尽管 last_seen 折秒 999 落窗外（反证）
        data_root, C3, FEED_A, create_time="1500", now_ms=999_000,
        media=[media_entry(URL_PENDING)],
    )
    write_comment(data_root, C4, FEED_A, create_time="2000")  # == to → 含入（半闭）
    write_comment(data_root, R1, FEED_A, create_time="1800")  # reply 亦按自身 createTime
    write_raw_comment(  # createTime 畸形 → skip+log
        data_root,
        R2,
        {"id": R2, "createTime": "not-a-number", "_p": minimal_p(FEED_A)},
    )

    caplog.set_level(logging.WARNING, logger="src.entity_store.scan")
    entries = list(iter_window(data_root, GUILD, 1000, 2000))

    assert sorted((e.kind, e.doc["id"]) for e in entries) == [
        ("comment", C3),
        ("comment", C4),
        ("reply", R1),
    ]
    by_id = {e.doc["id"]: e for e in entries}
    assert by_id[C3].path == comment_path(data_root, GUILD, C3)
    assert by_id[C4].path == comment_path(data_root, GUILD, C4)
    assert by_id[R1].path == comment_path(data_root, GUILD, R1)
    assert by_id[C3].doc["_p"]["media"][0]["url"] == URL_PENDING
    assert len(caplog.records) == 1
    assert str(comment_path(data_root, GUILD, R2)) in caplog.text


def test_iter_window_inversion_raises_and_missing_guild_is_empty(data_root):
    with pytest.raises(ValueError, match="from_ts < to_ts"):
        list(iter_window(data_root, GUILD, 2000, 1000))
    with pytest.raises(ValueError, match="from_ts < to_ts"):  # 零宽窗同样 fail loud
        list(iter_window(data_root, GUILD, 1000, 1000))
    with pytest.raises(ValueError, match="int unix seconds"):
        list(iter_window(data_root, GUILD, "1000", 2000))

    write_feed(data_root, FEED_A)

    missing_guild = "9999999999999999"
    assert scan(data_root, missing_guild) == ScanResult()
    assert list(iter_window(data_root, missing_guild, 1000, 2000)) == []
    assert scan(data_root, GUILD).feeds == 1  # 邻树互不干扰
