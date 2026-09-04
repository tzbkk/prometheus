"""Viewer backend API handlers — 契约面。

六指令边（contracts/components/viewer.yaml 投影）：

    GET  /api/feeds                    200 FeedList    {feeds: [11 字段投影 + first_media 附加]}
    GET  /api/search                   200 FeedList    （同结构双指令；LIKE 子串 = 行为规格）
    GET  /api/feed/{feed_id}           200 FeedDetail  （media.path 后端全路径）
    GET  /api/feed/{feed_id}/comments  200 CommentList
    GET  /api/guilds                   200 GuildList
    GET  /api/stats                    200 ViewerStats {feeds, comments, media}（单命名）
    POST /api/rebuild                  200 RebuildAck  {accepted: true}（异步重建，进度经 stats 轮询）

错误统一 ErrorEnvelope：裸状态码 + {"error": {"code", "message"}}。

create_time 契约钉十进制字符串秒（原体形态）——SQLite INTEGER 列（排序用）在
API 面格式化为 str（canonical 十进制，等价原体）。列表/详情条目的附加字段
（first_media/raw_json/…）走编译 Schema 的 additionalProperties 宽容面。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

HandlerResult = Tuple[int, Any]

# 契约 11 字段投影（FeedList.Table 逐字）；first_media 为附加宽容字段
_FEED_COLUMNS = (
    "id, guild_id, create_time, title_text, author_nick, author_id, "
    "author_avatar, like_count, comment_count, image_count, video_count"
)
_CONTENT_COLUMN = "content_text"


def error_envelope(code: str, message: str) -> dict:
    """ErrorEnvelope——唯一错误形。"""
    return {"error": {"code": code, "message": message}}


def media_href(guild_id: Optional[str], file: Optional[str]) -> Optional[str]:
    """媒体全路径 URL（后端单权威、前端零拼接）：/media/<guild>/<shard>/<file>。

    shard = 文件名前 2 位（MediaAsset Location 模板：内容寻址名即哈希、桶 = 摘要
    前 2 位——与 paths.media_dir 同源规则）。
    """
    if not guild_id or not file:
        return None
    return f"/media/{guild_id}/{file[:2]}/{file}"


def media_indexed(db_path: str, guild_id: str, file: str) -> bool:
    """两段兼容路由的反查：file 是否在该 guild 的已索引媒体面。

    media ∪ comment_media 两表并集（/media/* 二进制面行为层自由；
    两段 URL 只解析已索引媒体，索引是 viewer 的可浏览面真相）。
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM media WHERE guild_id = ? AND file = ? "
            "UNION SELECT 1 FROM comment_media WHERE guild_id = ? AND file = ? "
            "LIMIT 1",
            (guild_id, file, guild_id, file),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _get_param(params: Dict[str, List[str]], name: str,
               default: Optional[str] = None) -> Optional[str]:
    values = params.get(name)
    if values:
        return values[0]
    return default


def _parse_pagination(params: Dict[str, List[str]]) -> Tuple[int, int]:
    """page（≥1）/ size（1..100）解析；非法 → ValueError（调用方 400 ErrorEnvelope）。"""
    page_str = _get_param(params, "page", "1")
    size_str = _get_param(params, "size", "20")
    try:
        page = int(page_str)  # type: ignore[arg-type]
        size = int(size_str)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("page and size must be integers")
    if page < 1:
        raise ValueError("page must be >= 1")
    if size < 1 or size > 100:
        raise ValueError("size must be between 1 and 100")
    return page, size


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _feed_item(row: sqlite3.Row) -> dict:
    """SQLite 行 → FeedList 条目（11 钉扎字段 + first_media 附加）。"""
    item = {k: row[k] for k in (
        "id", "guild_id", "title_text", "author_nick", "author_id",
        "author_avatar", "like_count", "comment_count", "image_count",
        "video_count",
    )}
    item["create_time"] = (
        str(row["create_time"]) if row["create_time"] is not None else ""
    )
    item["first_media"] = media_href(row["guild_id"], row["first_media"])
    return item


def handle_feeds(db_path: str, query_params: Dict[str, List[str]]) -> HandlerResult:
    """GET /api/feeds?page=&size=&guild= → FeedList（newest-first 分页投影）。"""
    try:
        page, size = _parse_pagination(query_params)
    except ValueError as exc:
        return 400, error_envelope("bad_request", str(exc))

    offset = (page - 1) * size
    guild_id = _get_param(query_params, "guild")
    where_clause = "WHERE guild_id = ?" if guild_id is not None else ""
    params: list = ([guild_id] if guild_id is not None else []) + [size, offset]
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_FEED_COLUMNS}, "
            "(SELECT file FROM media WHERE feed_id = feeds.id "
            "ORDER BY rowid LIMIT 1) AS first_media "
            f"FROM feeds {where_clause} ORDER BY create_time DESC, id "
            "LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()
    return 200, {"feeds": [_feed_item(r) for r in rows]}


def handle_search(db_path: str, query_params: Dict[str, List[str]]) -> HandlerResult:
    """GET /api/search?q=&page=&size=&guild= → FeedList（LIKE 子串语义，行为规格）。

    FTS5 unicode61 不分词 CJK——子串搜索走 title_text/raw_json 的 LIKE
    （实证路径，FeedList Description 认可）。
    """
    q = (_get_param(query_params, "q", "") or "").strip()
    if not q:
        return 400, error_envelope("bad_request", "q parameter is required")
    try:
        page, size = _parse_pagination(query_params)
    except ValueError as exc:
        return 400, error_envelope("bad_request", str(exc))

    offset = (page - 1) * size
    pattern = f"%{q}%"
    guild_id = _get_param(query_params, "guild")
    sql = (
        f"SELECT {_FEED_COLUMNS}, NULL AS first_media FROM feeds "
        "WHERE (title_text LIKE ? OR content_text LIKE ? OR raw_json LIKE ?)"
    )
    params: list = [pattern, pattern, pattern]
    if guild_id is not None:
        sql += " AND guild_id = ?"
        params.append(guild_id)
    sql += " ORDER BY create_time DESC, id LIMIT ? OFFSET ?"
    params.extend([size, offset])

    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    return 200, {"feeds": [_feed_item(r) for r in rows]}


def handle_feed_detail(db_path: str, feed_id: str) -> HandlerResult:
    """GET /api/feed/{feed_id} → FeedDetail（media.path 全路径 + 宽容附加字段）。"""
    if not feed_id:
        return 404, error_envelope("not_found", "feed not found")
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {_FEED_COLUMNS}, {_CONTENT_COLUMN}, NULL AS first_media, "
            "raw_json, indexed_at FROM feeds WHERE id = ?",
            (feed_id,),
        ).fetchone()
        if row is None:
            return 404, error_envelope("not_found", f"feed {feed_id} not found")
        media_rows = conn.execute(
            "SELECT file, url, type FROM media WHERE feed_id = ? ORDER BY rowid",
            (feed_id,),
        ).fetchall()
    finally:
        conn.close()

    feed = _feed_item(row)
    try:
        feed["raw_json"] = (
            json.loads(row["raw_json"]) if row["raw_json"] else None
        )
    except json.JSONDecodeError:
        feed["raw_json"] = None
    feed["content_text"] = row["content_text"]
    feed["indexed_at"] = row["indexed_at"]
    feed["media"] = [
        {
            "path": media_href(feed["guild_id"], m["file"]),
            "type": m["type"],
            "url": m["url"],
        }
        for m in media_rows
    ]
    return 200, feed


def handle_feed_comments(db_path: str, feed_id: str) -> HandlerResult:
    """GET /api/feed/{feed_id}/comments → CommentList（钉扎 {id: CommentId, feed_id}）。

    契约面只容 c_ 前缀条目（CommentId 模式 ^c_…；回复是 ReplyId 另一结构，
    viewer 契约无 ReplyList 指令边）——r_ 回复不入本列表，
    仅计入 ViewerStats.comments（"含回复"）。实体
    树的 reply 文件只带 _p.feed_id 归属链、无父评论 id（父评论 id 不可再生）。
    """
    if not feed_id:
        return 404, error_envelope("not_found", "feed not found")
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, guild_id, create_time, author_nick, author_avatar, "
            "content_text, like_count, reply_count, sequence "
            "FROM comments WHERE feed_id = ? AND substr(id, 1, 2) = 'c_' "
            "ORDER BY COALESCE(sequence, 4294967295), create_time, id",
            (feed_id,),
        ).fetchall()
        comment_ids = [row["id"] for row in rows]
        media_by_comment: Dict[str, List[dict]] = {}
        if comment_ids:
            placeholders = ",".join("?" * len(comment_ids))
            media_rows = conn.execute(
                f"SELECT comment_id, guild_id, file, url, type, width, height "
                f"FROM comment_media WHERE comment_id IN ({placeholders})",
                tuple(comment_ids),
            ).fetchall()
            for m in media_rows:
                media_by_comment.setdefault(m["comment_id"], []).append({
                    "path": media_href(m["guild_id"], m["file"]),
                    "file": m["file"],
                    "url": m["url"],
                    "type": m["type"],
                    "width": m["width"],
                    "height": m["height"],
                })
    finally:
        conn.close()

    comments = [
        {
            "id": row["id"],
            "feed_id": feed_id,
            "create_time": row["create_time"],
            "author_nick": row["author_nick"],
            "author_avatar": row["author_avatar"],
            "content_text": row["content_text"],
            "like_count": row["like_count"],
            "reply_count": row["reply_count"],
            "sequence": row["sequence"],
            "media": media_by_comment.get(row["id"], []),
        }
        for row in rows
    ]
    return 200, {"comments": comments}


def handle_guilds(db_path: str) -> HandlerResult:
    """GET /api/guilds → GuildList（钉扎 {guild_id} + feeds 计数附加）。"""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT guild_id, feeds FROM guilds ORDER BY feeds DESC, guild_id"
        ).fetchall()
    finally:
        conn.close()
    return 200, {
        "guilds": [
            {"guild_id": r["guild_id"], "feeds": r["feeds"]} for r in rows
        ]
    }


def handle_stats(db_path: str) -> HandlerResult:
    """GET /api/stats → ViewerStats（单命名三键，comments 含回复）。

    media = 两表（feed 媒体 + 评论媒体）按内容寻址文件名去重的资产数——
    与磁盘媒体树同一语义（同文件跨实体引用不重复计数）。
    """
    conn = _connect(db_path)
    try:
        feed_count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        comment_count = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        media_count = conn.execute(
            "SELECT COUNT(*) FROM (SELECT file FROM media "
            "UNION SELECT file FROM comment_media)"
        ).fetchone()[0]
    finally:
        conn.close()
    return 200, {
        "feeds": feed_count,
        "comments": comment_count,
        "media": media_count,
    }
