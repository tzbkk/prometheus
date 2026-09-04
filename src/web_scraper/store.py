"""Entity-backed store for the web scraper.

核心机制（无记忆增长检测 + 实体树）：
    - 实体落盘 = entity_store.write_entity 直写（整文件原子重写）；
    - 去重 = 文件存在性（文件名即 id）；
    - 增长检测 = API commentCount vs 本地评论文件数（启动扫描派生内存索引，
      本模块维护 `_comment_files`——写侧增量更新，零记忆化文件）；
    - 死 URL 集 = scan().dead_urls 启动派生 + 写侧晋升回灌（in-memory）；
    - 媒体状态机改写一律经 write_entity（media 合并 + touch_clocks=False
      时钟冻结——媒体尝试不动实体观测时钟）。

fail loud at boundary（与 scan 宽容遍历语义对齐）：
    畸形腾讯载荷（id 前缀错 / createTime 非字符串 / 缺 channelInfo.sign /
    guild 归属不符 / WriterError 族）→ logging.warning + skip，绝不落盘、
    绝不猜测修复；边界外的程序性 bug（rewrite_media 无对象等）照常 raise。

媒体身份键 = urlnorm 归一 URL（dis_k/dis_t 易变参数剥除）——腾讯视频 CDN
每响应换签名参数，裸 URL 每次都是新身份，死集/合并将永久 miss（实证）。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from src.entity_store.paths import (
    PathFormatError,
    comment_path,
    feed_path,
)
from src.entity_store.reader import ReaderError, load_entity
from src.entity_store.writer import (
    DEAD_PROMOTION_RETRIES,
    WriterError,
    write_entity,
)
from src.web_scraper.urlnorm import normalize_media_url

__all__ = [
    "CAPTURED_VIA",
    "EntityStore",
    "growth_targets",
    "media_block",
]

logger = logging.getLogger(__name__)

CAPTURED_VIA = "scraper"

FEED_PREFIX = "B_"
COMMENT_PREFIXES = ("c_", "r_")

# save_* 返回的处置状态（skip+log 边界语义 vs 观测语义）
CREATED = "created"
REWRITTEN = "rewritten"
EXISTS = "exists"
SKIPPED = "skipped"


def growth_targets(api_counts: dict[str, int], local_counts: dict[str, int]) -> set[str]:
    """无记忆比较（纯函数，参数表的被测对象）。

    API 侧 commentCount（每次列表刷新现拿） > 本地侧该帖评论文件数
    （启动扫描派生内存索引 + 写侧增量） → 该帖入重拉集。
    本地无账（.get 0）且 API > 0 → 重拉；API ≤ 本地 → 不重拉。
    """
    return {
        fid
        for fid, api_cc in api_counts.items()
        if api_cc > local_counts.get(fid, 0)
    }


def media_block(
    url: str,
    *,
    media_type: str,
    width: int | None = None,
    height: int | None = None,
    status: str = "pending",
    download_url: str | None = None,
) -> dict:
    """9 字段块全钉形态（含 download_url），键序 = 契约表序
    （status 可覆写——死集种子用；download_url = 最近观测原签地址，
    无签名参数时与 url 同——下载器优先取它）。"""
    return {
        "url": url,
        "download_url": download_url if download_url is not None else url,
        "file": None,
        "type": media_type,
        "width": width,
        "height": height,
        "status": status,
        "retries": 0,
        "last_attempt_ts": None,
    }


def _int_or_none(value: object) -> int | None:
    """width/height 投影：int 直收（bool 排除），其余 → None（未测得）。"""
    return value if type(value) is int else None


def _effective_dead(entry: dict) -> bool:
    """写前晋升判据的镜像（writer 恒 promoted failed@retries>=3 → dead）。"""
    return entry["status"] == "dead" or (
        entry["status"] == "failed" and entry.get("retries", 0) >= DEAD_PROMOTION_RETRIES
    )


class EntityStore:
    """Per-guild 实体树写门面：writer 直写 + 内存派生索引（增长/死集）。

    线程安全：`_lock` 串行化一切实体写与索引更新（comments 抓取线程池下
    多 feed 并发写不同文件本可并行，锁换取索引一致性 + merge 无竞争，
    写盘本身是原子重写，锁内开销可接受）。
    """

    def __init__(
        self,
        data_root: Path | str,
        guild: str,
        *,
        dead_urls: set[str] | None = None,
        clock=None,
    ):
        """Args:
            data_root: 数据根（data/；实体树 data/<guild>/…）。
            guild: 数字 guild_id（树第一段，契约 GuildId）。
            dead_urls: 启动扫描派生死集种子（scan().dead_urls）。
            clock: () -> int unix ms（测试确定性注入；默认墙钟）。
        """
        self.data_root = Path(data_root)
        self.guild = str(guild)
        self.dead_urls: set[str] = set(dead_urls or ())
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        # 增长检测内存索引：feed_id → 该帖评论文件数（c_ + r_，
        # "评论文件数"取 comments/ 树全部归属文件——两种 commentCount
        # 口径下收敛）。
        self._comment_files: dict[str, int] = {}
        # 观测到 commentCount>0 的 feed 集（recheck 轮换池的另一半）。
        self._comment_interested: set[str] = set()
        # 进程内累计计数（/stats 快照来源；重启归零语义不钉）。
        self.created_feeds = 0
        self.created_comments = 0
        self.created_replies = 0

    # ------------------------------------------------------------------
    # 启动扫描种子（__main__ 每 guild 一次）
    # ------------------------------------------------------------------
    def seed_comment_counts(
        self, comment_counts: dict[str, int], reply_counts: dict[str, int]
    ) -> None:
        """scan() 产物灌入内存索引（c_ 与 r_ 文件数同账相加）。"""
        with self._lock:
            for fid, count in comment_counts.items():
                self._comment_files[fid] = self._comment_files.get(fid, 0) + count
            for fid, count in reply_counts.items():
                self._comment_files[fid] = self._comment_files.get(fid, 0) + count

    def local_comment_counts(self) -> dict[str, int]:
        """增长检测本地侧（无记忆比较的 in-memory 索引，零落盘）。"""
        with self._lock:
            return dict(self._comment_files)

    def feed_ids_with_comments(self) -> list[str]:
        """recheck 轮换池：已有评论文件的 feed ∪ 观测过 commentCount>0 的 feed。"""
        with self._lock:
            return sorted(self._comment_files.keys() | self._comment_interested)

    def now_ms(self) -> int:
        return self._clock()

    # ------------------------------------------------------------------
    # 实体写入（boundary fail loud：畸形 → skip+log 不落盘）
    # ------------------------------------------------------------------
    def save_feed(self, feed: object) -> str:
        """腾讯 feed 元素 → feed 实体（新见 CREATED / 重拉 REWRITTEN / 畸形 SKIPPED）。

        重拉语义：顶层逐字替换、first_seen 保留、last_seen 更新、
        media 只填不删——同 url 旧块逐字保留（状态机不重置），新 url 立块
        pending（死集命中直接 dead——"新条目直接标 dead"）。
        """
        fid = self._validate_feed(feed)
        if fid is None:
            return SKIPPED
        feed_dict: dict = feed
        cc = feed_dict.get("commentCount")
        if isinstance(cc, int) and cc > 0:
            self._comment_interested.add(fid)

        refs = self._feed_media_refs(feed_dict)
        with self._lock:
            existed = feed_path(self.data_root, self.guild, fid).exists()
            blocks = self._blocks_for(refs, fid)
            try:
                write_entity(
                    self.data_root,
                    self.guild,
                    fid,
                    feed_dict,
                    captured_via=CAPTURED_VIA,
                    media=blocks,
                    now_ms=self.now_ms(),
                )
            except (WriterError, PathFormatError, ReaderError) as exc:
                logger.warning("skip malformed feed payload %s: %s", fid, exc)
                return SKIPPED
            if not existed:
                self.created_feeds += 1
                return CREATED
            return REWRITTEN

    def save_comment(self, node: object, *, feed_id: str) -> str:
        """腾讯评论/回复元素 → c_/r_ 实体（新见 CREATED / 已存 EXISTS / 畸形 SKIPPED）。

        create-only：已存实体不重写（评论重拉的价值在新增，last_seen 不随
        recheck 轮换翻动——与 feed 的"观测即重写"差异见 decisions.md）。
        回复在父评论 vecReply 内嵌克隆由顶层 verbatim 携带，本方法只负责
        独立 r_ 文件（双存）。
        """
        cid = self._validate_comment(node, feed_id)
        if cid is None:
            return SKIPPED
        node_dict: dict = node
        refs = self._comment_media_refs(node_dict)

        with self._lock:
            path = comment_path(self.data_root, self.guild, cid)
            if path.exists():
                return EXISTS
            blocks = self._blocks_for(refs, cid)
            try:
                write_entity(
                    self.data_root,
                    self.guild,
                    cid,
                    node_dict,
                    captured_via=CAPTURED_VIA,
                    media=blocks,
                    now_ms=self.now_ms(),
                    feed_id=feed_id,
                )
            except (WriterError, PathFormatError, ReaderError) as exc:
                logger.warning("skip malformed comment payload %s: %s", cid, exc)
                return SKIPPED
            self._comment_files[feed_id] = self._comment_files.get(feed_id, 0) + 1
            if cid.startswith("c_"):
                self.created_comments += 1
            else:
                self.created_replies += 1
            return CREATED

    # ------------------------------------------------------------------
    # 媒体状态机改写（唯一入口经 writer；时钟冻结路径）
    # ------------------------------------------------------------------
    def media_blocks(self, entity_id: str) -> list[dict] | None:
        """实体当前 _p.media 块（条目浅拷贝）；无实体 → None。"""
        with self._lock:
            doc = self._load_doc(entity_id)
        if doc is None:
            return None
        return [dict(entry) for entry in doc["_p"]["media"]]

    def rewrite_media(self, entity_id: str, blocks: list[dict]) -> Path:
        """媒体尝试结果回写：body 取磁盘存量逐字，touch_clocks=False
        （两本时钟冻结——媒体尝试不动实体观测时钟）。

        writer 写前晋升 failed@retries>=3 → dead；本方法同步把晋升与既有
        dead 块的 url 灌回 in-memory 死集（下次任何实体引用直接标 dead）。
        无对象/存量损坏 → raise（程序性 bug，非腾讯输入面）。
        """
        with self._lock:
            doc = self._load_doc(entity_id)
            if doc is None:
                raise WriterError(f"rewrite_media requires an existing entity: {entity_id}")
            body = {key: value for key, value in doc.items() if key != "_p"}
            feed_id = doc["_p"].get("feed_id") if entity_id.startswith(COMMENT_PREFIXES) else None
            path = write_entity(
                self.data_root,
                self.guild,
                entity_id,
                body,
                captured_via=CAPTURED_VIA,
                media=blocks,
                now_ms=self.now_ms(),
                feed_id=feed_id,
                touch_clocks=False,
            )
            self.dead_urls |= {entry["url"] for entry in blocks if _effective_dead(entry)}
        return path

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _load_doc(self, entity_id: str) -> dict | None:
        """读实体（锁内调用）；不存在 → None；损坏 → ReaderError 上抛。"""
        path = (
            feed_path(self.data_root, self.guild, entity_id)
            if entity_id.startswith(FEED_PREFIX)
            else comment_path(self.data_root, self.guild, entity_id)
        )
        if not path.exists():
            return None
        return load_entity(path)

    def _blocks_for(
        self, refs: list[tuple[str, str, str, int | None, int | None]], entity_id: str
    ) -> list[dict]:
        """新观测引用 → 块清单：存量同 url 块逐字保留 + download_url 刷新
        （签名时效——每次观测都带新签）；新 url 立块（死集命中 →
        status=dead 直接终态，"新条目直接标 dead"）。

        存量块无 download_url 键且状态为 dead/failed → 刷新时复活为
        pending（有键的块真死不扰，判据 = 键存在与否）。
        """
        existing: dict[str, dict] = {}
        doc = self._load_doc(entity_id)
        if doc is not None:
            existing = {entry["url"]: entry for entry in doc["_p"]["media"]}
        blocks: list[dict] = []
        for url, raw_url, media_type, width, height in refs:
            old = existing.get(url)
            if old is not None:
                merged = dict(old)
                merged["download_url"] = raw_url
                if "download_url" not in old and merged.get("status") in ("dead", "failed"):
                    merged["status"] = "pending"
                blocks.append(merged)
                continue
            block = media_block(
                url,
                media_type=media_type,
                width=width,
                height=height,
                download_url=raw_url,
            )
            if url in self.dead_urls:
                block["status"] = "dead"
            blocks.append(block)
        return blocks

    def _validate_feed(self, feed: object) -> str | None:
        """feed 边界校验（Feed.yaml 薄钉面）：不过 → warning + None。"""
        if not isinstance(feed, dict):
            logger.warning("skip feed: payload is not an object (%s)", type(feed).__name__)
            return None
        fid = feed.get("id")
        if not isinstance(fid, str) or not fid.startswith(FEED_PREFIX):
            logger.warning("skip feed: id must be a B_-prefixed string, got %r", fid)
            return None
        create_time = feed.get("createTime")
        if not isinstance(create_time, str) or not create_time:
            logger.warning(
                "skip feed %s: createTime must be a decimal string, got %r",
                fid,
                create_time,
            )
            return None
        channel_info = feed.get("channelInfo")
        if not isinstance(channel_info, dict) or not isinstance(
            channel_info.get("sign"), dict
        ):
            logger.warning("skip feed %s: channelInfo.sign missing/malformed", fid)
            return None
        guild_id = channel_info["sign"].get("guild_id")
        if guild_id != self.guild:
            logger.warning(
                "skip feed %s: sign.guild_id %r does not belong to guild %r",
                fid,
                guild_id,
                self.guild,
            )
            return None
        return fid

    def _validate_comment(self, node: object, feed_id: str) -> str | None:
        """评论/回复边界校验（Comment/Reply.yaml 薄钉面）。"""
        if not isinstance(node, dict):
            logger.warning(
                "skip comment: payload is not an object (%s)", type(node).__name__
            )
            return None
        cid = node.get("id")
        if not isinstance(cid, str) or not cid.startswith(COMMENT_PREFIXES):
            logger.warning(
                "skip comment: id must be c_/r_-prefixed string, got %r", cid
            )
            return None
        create_time = node.get("createTime")
        if not isinstance(create_time, str) or not create_time:
            logger.warning(
                "skip comment %s: createTime must be a decimal string, got %r",
                cid,
                create_time,
            )
            return None
        if not isinstance(feed_id, str) or not feed_id.startswith(FEED_PREFIX):
            logger.warning(
                "skip comment %s: page-context feed_id must be B_-prefixed, got %r",
                cid,
                feed_id,
            )
            return None
        return cid

    @staticmethod
    def _feed_media_refs(feed: dict) -> list[tuple[str, str, str, int | None, int | None]]:
        """images[*].picUrl / videos[*].playUrl → (归一url, 原签url, type, w, h)。

        归一 url = 身份/死集键；原签 url = 下载用（视频 playUrl 的
        dis_k/dis_t 签名是下载必需，剥离即 404）。
        """
        refs: list[tuple[str, str, str, int | None, int | None]] = []
        for img in feed.get("images") or []:
            if not isinstance(img, dict):
                continue
            url = img.get("picUrl")
            if isinstance(url, str) and url:
                refs.append(
                    (
                        normalize_media_url(url),
                        url,
                        "image",
                        _int_or_none(img.get("width")),
                        _int_or_none(img.get("height")),
                    )
                )
        for vid in feed.get("videos") or []:
            if not isinstance(vid, dict):
                continue
            url = vid.get("playUrl")
            if isinstance(url, str) and url:
                refs.append(
                    (
                        normalize_media_url(url),
                        url,
                        "video",
                        _int_or_none(vid.get("width")),
                        _int_or_none(vid.get("height")),
                    )
                )
        return refs

    @staticmethod
    def _comment_media_refs(node: dict) -> list[tuple[str, str, str, int | None, int | None]]:
        """richContents.images[*].picUrl + sticker.custom_face.origin_image_url。"""
        refs: list[tuple[str, str, str, int | None, int | None]] = []
        rich = node.get("richContents")
        if not isinstance(rich, dict):
            return refs
        for img in rich.get("images") or []:
            if not isinstance(img, dict):
                continue
            url = img.get("picUrl")
            if isinstance(url, str) and url:
                refs.append(
                    (
                        normalize_media_url(url),
                        url,
                        "image",
                        _int_or_none(img.get("width")),
                        _int_or_none(img.get("height")),
                    )
                )
        sticker = rich.get("sticker")
        if isinstance(sticker, dict):
            face = sticker.get("custom_face")
            if isinstance(face, dict):
                url = face.get("origin_image_url")
                if isinstance(url, str) and url:
                    refs.append(
                        (
                            normalize_media_url(url),
                            url,
                            "sticker",
                            _int_or_none(face.get("pic_width")),
                            _int_or_none(face.get("pic_height")),
                        )
                    )
        return refs
