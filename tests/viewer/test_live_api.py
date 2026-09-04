"""活体六指令边 + 负例 + rebuild 刷新 mandate（MD-054/055/056）。

MD-054（pillar 对应面）：七路由六结构（feeds/search 同 FeedList、detail、comments、
guilds、stats、rebuild）逐一过编译 Schema；nullable 字段实例与 media.path 全路径。
MD-055（pillar 对应面）：空 data 根 → 200 空列表/零计数（优雅降级非 500）；
404/400/405 一律 ErrorEnvelope。
MD-056（pillar 对应面）：活体写新实体 → rebuild 受理 → 轮询 stats 至新计数 →
feeds 可见——索引非事实源、可随时从树再生。
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

from src.entity_store import write_entity

from tests.viewer.conftest import (
    COMMENT_1,
    FEED_A,
    FEED_BARE,
    FEED_C,
    FEED_NEW,
    GUILD_A,
    GUILD_B,
    PNG_A_NAME,
    T2,
    _feed_body,
    live_viewer,
)


def test_live_six_endpoints_conform_contracted_schemas(viewer_site, http, schema_assert):
    base = viewer_site.base

    # FeedList（分页投影 + nullable 实例 + first_media 全路径附加）
    status, body = http("GET", f"{base}/api/feeds?size=10")
    assert status == 200
    schema_assert(body, "FeedList")
    assert [item["id"] for item in body["feeds"]] == [FEED_BARE, FEED_A, FEED_C]
    by_id = {item["id"]: item for item in body["feeds"]}
    bare = by_id[FEED_BARE]
    assert bare["title_text"] is None
    assert bare["author_nick"] is None and bare["author_id"] is None
    assert bare["author_avatar"] is None
    full = by_id[FEED_A]
    assert full["title_text"] == "合成帖子标题 第二段"
    assert full["author_nick"] == "作者甲"
    assert full["like_count"] == 5 and full["comment_count"] == 2
    assert full["image_count"] == 2 and full["video_count"] == 1
    assert full["create_time"] == "1782919600"  # 契约钉十进制字符串秒（原体形态）
    assert full["first_media"] == f"/media/{GUILD_A}/{PNG_A_NAME[:2]}/{PNG_A_NAME}"

    # search（同结构双指令边——FeedList；查询串须百分号编码——http.client ASCII 面）
    status, body = http("GET", f"{base}/api/search?q={quote('频道B')}")
    assert status == 200
    schema_assert(body, "FeedList")
    assert [item["id"] for item in body["feeds"]] == [FEED_C]

    # FeedDetail（media.path 后端全路径，㉒-c）
    status, body = http("GET", f"{base}/api/feed/{FEED_A}")
    assert status == 200
    schema_assert(body, "FeedDetail")
    assert body["create_time"] == "1782919600"
    assert [m["path"] for m in body["media"]] == [
        f"/media/{GUILD_A}/{PNG_A_NAME[:2]}/{PNG_A_NAME}"
    ]
    assert body["raw_json"]["id"] == FEED_A

    # CommentList（钉扎 {id: CommentId, feed_id}；文本/作者投影附加字段）
    # 契约面只容 c_ 条目——r_ 回复是 ReplyId 另一结构（D2 分治），不入列表
    status, body = http("GET", f"{base}/api/feed/{FEED_A}/comments")
    assert status == 200
    schema_assert(body, "CommentList")
    entries = {c["id"]: c for c in body["comments"]}
    assert set(entries) == {COMMENT_1}
    top = entries[COMMENT_1]
    assert top["feed_id"] == FEED_A
    assert top["content_text"] == "评论一段 评论二段"
    assert top["author_nick"] == "评论者乙"
    assert top["like_count"] == 3  # likeInfo.count 字符串 "3" → 计数容错
    assert top["media"][0]["path"].startswith(f"/media/{GUILD_A}/")

    # GuildList
    status, body = http("GET", f"{base}/api/guilds")
    assert status == 200
    schema_assert(body, "GuildList")
    guilds = {g["guild_id"]: g["feeds"] for g in body["guilds"]}
    assert guilds == {GUILD_A: 2, GUILD_B: 1}

    # ViewerStats（㉒-d 单命名三键）
    status, body = http("GET", f"{base}/api/stats")
    assert status == 200
    schema_assert(body, "ViewerStats")
    assert body == {"feeds": 3, "comments": 2, "media": 2}

    # RebuildAck（受理语义）
    status, body = http("POST", f"{base}/api/rebuild")
    assert status == 200
    schema_assert(body, "RebuildAck")
    assert body["accepted"] is True


def test_live_empty_tree_serves_200_and_error_envelope_negatives(
    data_root, http, schema_assert
):
    db_path = Path(data_root).parent / "empty-viewer.db"
    with live_viewer(db_path, data_root) as server:
        base = f"http://127.0.0.1:{server.port}"

        # 空 data 根（无 guild 目录）→ 200 空列表/零计数（优雅降级非 500）
        status, body = http("GET", f"{base}/api/feeds")
        assert status == 200
        schema_assert(body, "FeedList")
        assert body == {"feeds": []}

        status, body = http("GET", f"{base}/api/guilds")
        assert status == 200
        schema_assert(body, "GuildList")
        assert body == {"guilds": []}

        status, body = http("GET", f"{base}/api/stats")
        assert status == 200
        schema_assert(body, "ViewerStats")
        assert body == {"feeds": 0, "comments": 0, "media": 0}

        status, body = http("GET", f"{base}/api/feed/{FEED_A}/comments")
        assert status == 200
        schema_assert(body, "CommentList")
        assert body == {"comments": []}

        # 负例：404/400/405 一律 ErrorEnvelope
        status, body = http("GET", f"{base}/api/feed/B_nosuchfeed000000000000000000")
        assert status == 404
        schema_assert(body, "ErrorEnvelope")
        assert body["error"]["code"] == "not_found"

        status, body = http("GET", f"{base}/api/nope")
        assert status == 404
        schema_assert(body, "ErrorEnvelope")

        status, body = http("GET", f"{base}/api/feeds?page=0")
        assert status == 400
        schema_assert(body, "ErrorEnvelope")
        assert body["error"]["code"] == "bad_request"

        status, body = http("GET", f"{base}/api/feeds?size=1000")
        assert status == 400
        schema_assert(body, "ErrorEnvelope")

        status, body = http("GET", f"{base}/api/search")
        assert status == 400
        schema_assert(body, "ErrorEnvelope")

        status, body = http("GET", f"{base}/api/rebuild")
        assert status == 405
        schema_assert(body, "ErrorEnvelope")
        assert body["error"]["code"] == "method_not_allowed"


def test_rebuild_endpoint_refreshes_index_for_newly_written_entities(
    viewer_site, http, schema_assert
):
    site = viewer_site

    # 活体写入新实体（writer 直写树——scraper 面同款入口）
    write_entity(
        site.data_root,
        GUILD_A,
        FEED_NEW,
        _feed_body(
            FEED_NEW,
            GUILD_A,
            "1782940000",
            title={"contents": [{"text_content": {"text": "rebuild 后的新帖"}}]},
        ),
        captured_via="scraper",
        media=[],
        now_ms=T2,
    )

    status, body = http("POST", f"{site.base}/api/rebuild")
    assert status == 200
    schema_assert(body, "RebuildAck")
    assert body["accepted"] is True

    stats = {}
    deadline = time.time() + 5
    while time.time() < deadline:
        _, stats = http("GET", f"{site.base}/api/stats")
        if stats.get("feeds") == site.counts.feeds + 1:
            break
        time.sleep(0.02)
    assert stats.get("feeds") == site.counts.feeds + 1  # 轮询至索引刷新

    status, body = http("GET", f"{site.base}/api/feeds?size=100")
    assert status == 200
    ids = [item["id"] for item in body["feeds"]]
    assert FEED_NEW in ids and ids.index(FEED_NEW) == 0  # 最新帖居首

    # 再受理一次（幂等语义）
    status, body = http("POST", f"{site.base}/api/rebuild")
    assert status == 200 and body["accepted"] is True


def test_live_feed_detail_carries_full_body_content_text(viewer_site, http):
    """MD-139：详情附加字段 content_text = 顶层 contents 全文（预览之外）。"""
    status, body = http("GET", f"{viewer_site.base}/api/feed/{FEED_A}")
    assert status == 200
    assert body["content_text"] == "合成帖子标题 第二段 这是被预览截断的完整正文尾部"
    assert "这是被预览截断的完整正文尾部" not in (body["title_text"] or "")
