"""三级对齐 mandate（MD-053，pillar 对应面）。

树（scan() 独立代码路径计数）= 索引（SQLite 行数）= API（活体分页累计 +
stats 计数）——三条独立实现路径互相印证，防索引面与树漂移。
"""

from __future__ import annotations

import sqlite3

from src.entity_store import scan

from tests.viewer.conftest import FEED_C, GUILD_A, GUILD_B


def test_tree_index_api_three_level_alignment(viewer_site, http):
    site = viewer_site

    # 树级：scan() 独立计数（与 indexer 不同代码路径——对齐才是强证）
    tree_feeds = 0
    tree_comments = 0  # c_ + r_（ViewerStats "含回复" 口径）
    tree_top_comments = 0  # 仅 c_（CommentList 契约面口径——CommentId ^c_…，D2 分治）
    for guild in (GUILD_A, GUILD_B):
        result = scan(site.data_root, guild)
        tree_feeds += result.feeds
        tree_comments += sum(result.comment_counts.values()) + sum(
            result.reply_counts.values()
        )
        tree_top_comments += sum(result.comment_counts.values())
    tree_media = len(
        [p for p in (site.data_root / GUILD_A / "media").rglob("*") if p.is_file()]
    )
    assert (tree_feeds, tree_comments, tree_media) == (3, 2, 2)

    # 索引级：SQLite 行数（media 与 stats 同语义——两表内容寻址名去重）
    conn = sqlite3.connect(site.db_path)
    try:
        index_feeds = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        index_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        index_media = conn.execute(
            "SELECT COUNT(*) FROM (SELECT file FROM media "
            "UNION SELECT file FROM comment_media)"
        ).fetchone()[0]
    finally:
        conn.close()
    assert (index_feeds, index_comments, index_media) == (
        tree_feeds,
        tree_comments,
        tree_media,
    )

    # API 级：stats 计数 + feeds 分页累计 + comments 累计
    status, body = http("GET", f"{site.base}/api/stats")
    assert status == 200
    assert body == {
        "feeds": tree_feeds,
        "comments": tree_comments,
        "media": tree_media,
    }

    seen: list[str] = []
    page = 1
    while True:
        status, body = http("GET", f"{site.base}/api/feeds?page={page}&size=2")
        assert status == 200
        batch = body["feeds"]
        seen.extend(item["id"] for item in batch)
        if len(batch) < 2:
            break
        page += 1
    assert len(seen) == len(set(seen)) == tree_feeds

    api_comments = 0
    for feed_id in seen:
        status, body = http("GET", f"{site.base}/api/feed/{feed_id}/comments")
        assert status == 200
        api_comments += len(body["comments"])
    # CommentList 面只计 c_（契约 CommentId 模式）；stats 面含回复——两口径各对齐
    assert api_comments == tree_top_comments
    status, body = http("GET", f"{site.base}/api/stats")
    assert status == 200 and body["comments"] == tree_comments

    # guild 过滤面对齐（GUILD_B 单帖）
    status, body = http("GET", f"{site.base}/api/feeds?guild={GUILD_B}&size=100")
    assert status == 200
    assert [item["id"] for item in body["feeds"]] == [FEED_C]
