"""派生索引启动扫描 + 归档时间窗迭代器。

一套机制两用（"无记忆比较"）：
1. scan(data_root, guild) 一趟扫 guild 树，纯内存产出——
   - per-feed comment/reply 计数（增长检测输入：API commentCount vs 本地计数 → 重拉集）；
   - dead-URL 集（_p.media status=dead 的 url——死媒体状态内嵌于实体 _p.media，
     scraper 重试前先查此集免徒劳）。
2. iter_window(data_root, guild, from_ts, to_ts) yield createTime ∈ (from_ts, to] 的实体
   （archive 消费——实体树切片；媒体并集由包引擎从窗口实体 _p.media 派生）。

窗口判据 = 腾讯业务时钟 createTime（三类实体各自按自身 createTime 判窗——reply 亦然；
_p.first_seen/last_seen 专职增量检测，不参与归档窗口）。createTime 投影唯一居所 =
reader.created_at_of，本模块不造第二投影。

宽容遍历（与 reader.load_entity 严格加载的**有意**差异——边界严格性归 load 侧，本差异是
设计而非疏漏）：启动扫描面对的是磁盘现状，个别损坏文件不应炸掉服务启动（损坏由审计
脚本点名）。故树中损坏 JSON / 缺 _p / 错分片桶 / 缺 _p.feed_id / 畸形 createTime →
logging.warning + skipped 计数，扫描继续、绝不 raise。

纯内存（派生态即算即用，零派生态持久化）：产物 dict/set/迭代器返回，零落盘、
零网络 IO、零时钟读取（窗口参数与判窗字段均由调用方/实体自带）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from src.entity_store.paths import PathFormatError, ParsedEntity, resolve
from src.entity_store.reader import ReaderError, created_at_of, load_entity

__all__ = [
    "ScanResult",
    "WindowEntry",
    "scan",
    "iter_window",
]

logger = logging.getLogger(__name__)

_DEAD_STATUS = "dead"


@dataclass(frozen=True)
class ScanResult:
    """scan() 纯内存产物（零落盘）。

    - comment_counts / reply_counts：feed_id → 该帖评论/回复文件数（增长检测：
      API commentCount vs 本地计数 → 重拉集；无该帖评论的 feed 不在键中，消费方 .get(fid, 0)）。
    - dead_urls：全树 _p.media 中 status=dead 的 url 集（feed 与 comment/reply 媒体皆入）。
    - pending_media：entity_id → _p.media 中 status∈{pending,failed} 的条目数
      （池化种子面——启动扫描发现的存量待试媒体实体，scraper 启动即入池；
      ok/dead 终态与全终态实体不入）。
    - feeds：合法 feed 文件计数（对账/观测用）。
    - skipped：被跳过的畸形文件计数（文件粒度——一文件任一检查不过即整体不入账）。
    """

    comment_counts: dict[str, int] = field(default_factory=dict)
    reply_counts: dict[str, int] = field(default_factory=dict)
    dead_urls: set[str] = field(default_factory=set)
    pending_media: dict[str, int] = field(default_factory=dict)
    feeds: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class WindowEntry:
    """iter_window() 产出：实体路径 + kind + 已加载文档（archive 切片镜像与媒体并集消费）。"""

    path: Path
    kind: str  # "feed" | "comment" | "reply"
    doc: dict


def _entity_paths(data_root: Path | str, guild: str) -> list[Path]:
    """guild 树下 feeds/ + comments/ 的全部文件（排序保确定性）。

    media/ 二进制树不读——死媒体状态内嵌于实体 _p.media，扫描零二进制 IO。
    guild 根 / kind 目录不存在 → 空清单（fresh install 语义，零异常）。
    """
    root = Path(data_root) / guild
    paths: list[Path] = []
    for kind_dir in ("feeds", "comments"):
        directory = root / kind_dir
        if directory.is_dir():
            paths.extend(p for p in directory.rglob("*") if p.is_file())
    return sorted(paths)


def _read_entity(path: Path) -> tuple[ParsedEntity, dict] | None:
    """宽容读取单实体文件：路径逆解析 + 严格加载 + id↔文件名一致性。

    任何违例（畸形路径 / 损坏 JSON / 缺 _p / id 不符）→ logging.warning + None，
    绝不 raise（宽容遍历；严格性归 load_entity 侧，损坏由审计脚本点名）。
    """
    try:
        parsed = resolve(path)
    except PathFormatError as exc:
        logger.warning("scan skip (malformed path): %s — %s", path, exc)
        return None
    try:
        doc = load_entity(path)
    except ReaderError as exc:
        logger.warning("scan skip (malformed entity): %s — %s", path, exc)
        return None
    if doc.get("id") != parsed.id:
        logger.warning(
            "scan skip (id mismatch): %s carries id %r, expected %r",
            path,
            doc.get("id"),
            parsed.id,
        )
        return None
    return parsed, doc


def _dead_urls_of(doc: dict, path: Path) -> set[str] | None:
    """_p.media 中 status=dead 的 url 集；media 非数组或条目畸形 → None（文件级不入账）。

    load_entity 只保证 _p 存在，不保证 _p.media——writer 纪律恒写 media 数组，
    缺失/非数组/条目缺 url 即契约违例，整文件 skip（与计数归账同粒度，不留半账）。
    """
    media = doc["_p"].get("media")
    if not isinstance(media, list):
        logger.warning("scan skip (_p.media not an array): %s", path)
        return None
    dead: set[str] = set()
    for entry in media:
        url = entry.get("url") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url:
            logger.warning("scan skip (malformed _p.media entry): %s", path)
            return None
        if entry.get("status") == _DEAD_STATUS:
            dead.add(url)
    return dead


def scan(data_root: Path | str, guild: str) -> ScanResult:
    """一趟扫 guild 树 → per-feed comment/reply 计数 + dead-URL 集（纯内存）。

    一机制两用的正主：scraper 启动时调用一次，产物驻内存供增长检测对比
    （API commentCount > 本地 comment_counts[fid] → 重拉）与死 URL 先查。
    畸形文件 skip+log（见模块 docstring 宽容遍历语义），skipped 计数入结果。
    """
    comment_counts: dict[str, int] = {}
    reply_counts: dict[str, int] = {}
    dead_urls: set[str] = set()
    pending_media: dict[str, int] = {}
    feeds = 0
    skipped = 0

    for path in _entity_paths(data_root, guild):
        entry = _read_entity(path)
        if entry is None:
            skipped += 1
            continue
        parsed, doc = entry

        dead = _dead_urls_of(doc, path)
        if dead is None:
            skipped += 1
            continue

        retryable = sum(
            1
            for media_entry in doc["_p"]["media"]
            if media_entry.get("status") in ("pending", "failed")
        )
        if retryable:
            pending_media[parsed.id] = retryable

        if parsed.kind == "feed":
            feeds += 1
        else:
            feed_id = doc["_p"].get("feed_id")
            if not isinstance(feed_id, str) or not feed_id:
                logger.warning(
                    "scan skip (comment/reply lacks _p.feed_id): %s", path
                )
                skipped += 1
                continue
            counts = comment_counts if parsed.kind == "comment" else reply_counts
            counts[feed_id] = counts.get(feed_id, 0) + 1

        dead_urls |= dead

    return ScanResult(
        comment_counts=comment_counts,
        reply_counts=reply_counts,
        dead_urls=dead_urls,
        pending_media=pending_media,
        feeds=feeds,
        skipped=skipped,
    )


def iter_window(
    data_root: Path | str, guild: str, from_ts: int, to_ts: int
) -> Iterator[WindowEntry]:
    """(from_ts, to_ts] 时间窗实体迭代器（archive 消费）。

    - 判据 = 实体自身 createTime（int 秒，经 reader.created_at_of——投影唯一居所）；
      _p.first_seen/last_seen（观测时钟）与归档窗无关。
    - 半开半闭逐字：createTime == from_ts 排除，== to_ts 含入。
    - from_ts >= to_ts（倒序/零宽）→ ValueError（fail loud——静默空窗会掩盖调用方 bug；
      "越界未来"等业务窗校验归 archive 服务边界，exit 2）。
    - createTime 畸形实体 → skip+log（宽容遍历，同 scan）。
    """
    if type(from_ts) is not int or type(to_ts) is not int:
        raise ValueError(
            f"window bounds must be int unix seconds, got ({from_ts!r}, {to_ts!r})"
        )
    if from_ts >= to_ts:
        raise ValueError(
            f"window must satisfy from_ts < to_ts (half-open (from, to]), "
            f"got ({from_ts}, {to_ts}]"
        )

    for path in _entity_paths(data_root, guild):
        entry = _read_entity(path)
        if entry is None:
            continue
        parsed, doc = entry
        try:
            created = created_at_of(doc)
        except ReaderError as exc:
            logger.warning("window skip (malformed createTime): %s — %s", path, exc)
            continue
        if from_ts < created <= to_ts:
            yield WindowEntry(path=path, kind=parsed.kind, doc=doc)
