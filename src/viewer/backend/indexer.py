"""SQLite indexer — 实体树目录扫描。

数据源：data_root 下 guild 目录树，经 scan 的 iter_window 公共迭代器全窗遍历
（(0, _MAX_TS] 恒真窗 = 目录扫描语义）——单一遍历产出 feeds/feeds_fts/media/
comments/comment_media 五表行。宽容语义与 scan 一致（畸形实体 skip+log 不炸索引；
损坏由审计脚本点名）。

增量策略 = 周期性全量重建：实体树无 append
语义，mtime 增量需状态账本；43k 实体扫描实测
~1.1s + SQLite 批写秒级，全量重建最简——派生索引可随时从树再生（索引
不是事实源）。

投影纪律：title/poster/text/author/created_at/media 六投影全部
import reader（投影唯一合法居所，viewer 内零自造投影）；like/comment/image/video
计数与 id/feed_id/sequence 等逐字透传是索引簿记而非实体投影，不属 reader 面。

FTS5/SQLite 内部不变（schema.py 原样复用）；per-guild DELETE+INSERT 使重建
对查询面原子可见（guild 目录暂缺时其已有行保留可浏览）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

from src.entity_store.paths import PathFormatError, media_path
from src.entity_store.reader import (
    ReaderError,
    author_of,
    body_of,
    created_at_of,
    media_of,
    poster_of,
    text_of,
    title_of,
)
from src.entity_store.scan import WindowEntry, iter_window
from src.viewer.backend.schema import init_db

__all__ = ["Indexer", "discover_guilds"]

logger = logging.getLogger(__name__)

_MAX_TS = 1 << 62  # 恒真上界（~10^11 年）——iter_window (0, MAX] = 全树


def discover_guilds(data_root) -> List[Tuple[str, Path]]:
    """data_root 直接子目录含 feeds/ 或 comments/ 子树 → [(guild_id, dir), ...]。

    guild 目录名须为纯数字（GuildId 契约 ^[0-9]+$）；prometheus.lock 等杂项文件
    与非数字目录跳过。data_root 不存在 → 空（fresh install：空索引零异常）。
    """
    root = Path(data_root)
    if not root.is_dir():
        return []
    result: List[Tuple[str, Path]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not entry.name.isnumeric():
            continue
        if (entry / "feeds").is_dir() or (entry / "comments").is_dir():
            result.append((entry.name, entry))
    return result


def _to_int(value: object, default: int = 0) -> int:
    """腾讯计数容错读取（可为十进制字符串；bool/None/垃圾 → default）。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _list_len(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


class Indexer:
    """实体树 → SQLite/FTS5 派生索引（只读消费者的索引面，可随时从树再生）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def rebuild_all(self, data_root) -> dict:
        """全量重建全部发现的 guild；返回 {feeds, comments, media} 行计数。"""
        conn = init_db(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        totals = {"feeds": 0, "comments": 0, "media": 0}
        try:
            for guild, _guild_dir in discover_guilds(data_root):
                counts = self._rebuild_guild(conn, data_root, guild)
                for key in totals:
                    totals[key] += counts[key]
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("indexed_at", datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        return totals

    def _rebuild_guild(self, conn: sqlite3.Connection, data_root, guild: str) -> dict:
        """单 guild DELETE+INSERT（其余 guild 行不动——暂缺目录的 guild 保留可浏览）。"""
        now = datetime.now(timezone.utc).isoformat()
        # feeds_fts 的清除须先于 feeds（子查询引用 feeds 行）
        conn.execute(
            "DELETE FROM feeds_fts WHERE feed_id IN "
            "(SELECT id FROM feeds WHERE guild_id = ?)",
            (guild,),
        )
        conn.execute("DELETE FROM comment_media WHERE guild_id = ?", (guild,))
        conn.execute("DELETE FROM media WHERE guild_id = ?", (guild,))
        conn.execute("DELETE FROM comments WHERE guild_id = ?", (guild,))
        conn.execute("DELETE FROM feeds WHERE guild_id = ?", (guild,))

        feed_rows: list[tuple] = []
        fts_rows: list[tuple] = []
        media_rows: list[tuple] = []
        comment_rows: list[tuple] = []
        comment_media_rows: list[tuple] = []
        skipped = 0

        for entry in iter_window(data_root, guild, 0, _MAX_TS):
            if entry.kind == "feed":
                built = self._feed_rows(data_root, guild, entry, now)
                if built is None:
                    skipped += 1
                    continue
                feed_rows.extend(built[0])
                fts_rows.extend(built[1])
                media_rows.extend(built[2])
            else:
                built = self._comment_rows(data_root, guild, entry)
                if built is None:
                    skipped += 1
                    continue
                comment_rows.extend(built[0])
                comment_media_rows.extend(built[1])

        if skipped:
            logger.warning(
                "indexer: skipped %d malformed entities in guild %s", skipped, guild
            )

        # FK 纪律：feeds 先于 media（PRAGMA foreign_keys = ON）
        if feed_rows:
            conn.executemany(
                "INSERT INTO feeds "
                "(id, guild_id, create_time, title_text, content_text, author_nick, "
                "author_id, author_avatar, like_count, comment_count, image_count, "
                "video_count, raw_json, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                feed_rows,
            )
        if fts_rows:
            conn.executemany(
                "INSERT INTO feeds_fts(feed_id, title_text, content_text, raw_json) "
                "VALUES (?,?,?,?)",
                fts_rows,
            )
        if media_rows:
            conn.executemany(
                "INSERT INTO media(feed_id, file, url, type, size, guild_id) "
                "VALUES (?,?,?,?,?,?)",
                media_rows,
            )
        if comment_rows:
            conn.executemany(
                "INSERT INTO comments "
                "(id, feed_id, guild_id, parent_id, create_time, author_nick, "
                "author_avatar, content_text, ip_location, like_count, reply_count, "
                "sequence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                comment_rows,
            )
        if comment_media_rows:
            conn.executemany(
                "INSERT INTO comment_media "
                "(comment_id, file, url, type, width, height, size, guild_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                comment_media_rows,
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM feeds WHERE guild_id = ?", (guild,)
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO guilds (guild_id, feeds, indexed_at) "
            "VALUES (?, ?, ?)",
            (guild, count, now),
        )
        conn.commit()
        return {
            "feeds": len(feed_rows),
            "comments": len(comment_rows),
            "media": len(media_rows),
        }

    def _feed_rows(
        self, data_root, guild: str, entry: WindowEntry, now: str
    ) -> tuple[list[tuple], list[tuple], list[tuple]] | None:
        """feed 实体 → (feed_rows, fts_rows, media_rows)；投影切片畸形 → None（skip+log）。"""
        doc = entry.doc
        try:
            create_time = created_at_of(doc)
            title = title_of(doc)
            body = body_of(doc)
            poster = poster_of(doc) or {"id": None, "nick": None, "avatar_url": None}
            media_list = media_of(doc)
        except ReaderError as exc:
            logger.warning(
                "indexer skip (malformed feed projection): %s — %s", entry.path, exc
            )
            return None

        feed_id = doc.get("id")
        if not isinstance(feed_id, str) or not feed_id:
            logger.warning("indexer skip (feed lacks id): %s", entry.path)
            return None

        total_like = doc.get("total_like")
        like_count = (
            _to_int(total_like.get("like_count")) if isinstance(total_like, dict) else 0
        )
        raw_json = json.dumps(doc, ensure_ascii=False)
        feed_row = (
            feed_id,
            guild,
            create_time,
            title,
            body,
            poster["nick"],
            poster["id"],
            poster["avatar_url"],
            like_count,
            _to_int(doc.get("commentCount")),
            _list_len(doc.get("images")),
            _list_len(doc.get("videos")),
            raw_json,
            now,
        )
        fts_row = (feed_id, title or "", body or "", raw_json)
        media_rows = self._media_rows(
            data_root, guild, feed_id, media_list, entry.path, is_comment=False
        )
        if media_rows is None:
            return None
        return [feed_row], [fts_row], media_rows

    def _comment_rows(
        self, data_root, guild: str, entry: WindowEntry
    ) -> tuple[list[tuple], list[tuple]] | None:
        """comment/reply 实体 → (comment_rows, comment_media_rows)；畸形 → None。"""
        doc = entry.doc
        feed_id = doc.get("_p", {}).get("feed_id")
        if not isinstance(feed_id, str) or not feed_id:
            logger.warning(
                "indexer skip (comment/reply lacks _p.feed_id): %s", entry.path
            )
            return None
        try:
            create_time = created_at_of(doc)
            author = author_of(doc)
            content_text = text_of(doc)
            media_list = media_of(doc)
        except ReaderError as exc:
            logger.warning(
                "indexer skip (malformed comment projection): %s — %s",
                entry.path,
                exc,
            )
            return None

        comment_id = doc.get("id")
        if not isinstance(comment_id, str) or not comment_id:
            logger.warning("indexer skip (comment lacks id): %s", entry.path)
            return None

        like_info = doc.get("likeInfo")
        like_count = _to_int(like_info.get("count")) if isinstance(like_info, dict) else 0
        # parent_id：实体树不可再生——reply 文件只带 _p.feed_id 归属链，父评论 id
        # 不在 _p 面（评论嵌套展示归前端宽容面）。
        comment_row = (
            comment_id,
            feed_id,
            guild,
            None,
            create_time,
            author["nick"],
            author["avatar_url"],
            content_text,
            None,
            like_count,
            _to_int(doc.get("replyCount")),
            _to_int(doc.get("sequence")) or None,
        )
        media_rows = self._media_rows(
            data_root, guild, comment_id, media_list, entry.path, is_comment=True
        )
        if media_rows is None:
            return None
        return [comment_row], media_rows

    @staticmethod
    def _media_rows(
        data_root, guild, owner_id, media_list, path, *, is_comment
    ) -> list[tuple] | None:
        """_p.media（reader.media_of 投影）→ 索引行；仅 file 非空条目（可服务面）。

        file 违反内容寻址语法（MediaAsset grammar，经 media_path fail-loud 面校验）
        → 该条目 skip+log（条目级畸形不殃及整实体，粒度宽于 scan 的文件级——
        索引面只关心可服务文件名，记录在案）。条目非对象 → 整文件 skip（None）。
        """
        rows: list[tuple] = []
        for index, m in enumerate(media_list):
            if not isinstance(m, dict):
                logger.warning(
                    "indexer skip (_p.media[%d] not an object): %s", index, path
                )
                return None
            file = m.get("file")
            if not isinstance(file, str) or not file:
                continue  # pending/未下载——不可服务不入索引
            try:
                media_path(data_root, guild, file)  # grammar + 桶位 fail-loud 校验
            except PathFormatError as exc:
                logger.warning(
                    "indexer skip (media file violates grammar): %s — %s", path, exc
                )
                continue
            url = m.get("url") if isinstance(m.get("url"), str) else None
            mtype = m.get("type") if isinstance(m.get("type"), str) else None
            if is_comment:
                rows.append(
                    (
                        owner_id,
                        file,
                        url,
                        mtype,
                        m.get("width") if isinstance(m.get("width"), int) else None,
                        m.get("height") if isinstance(m.get("height"), int) else None,
                        None,
                        guild,
                    )
                )
            else:
                rows.append((owner_id, file, url, mtype, None, guild))
        return rows
