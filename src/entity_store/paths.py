"""实体路径分片/构造/逆解析（契约唯一规格：structures/{Feed,Comment,Reply,MediaAsset}.yaml 的 Location 模板，逐字对齐）。

契约模板：
- Feed:    file:data/{guild}/feeds/B_{feed_shard}/{feed_id}.json
- Comment: file:data/{guild}/comments/c_{comment_shard}/{comment_id}.json
- Reply:   file:data/{guild}/comments/r_{reply_shard}/{reply_id}.json
- Media:   file:data/{guild}/media/{media_shard}/{media_file}

分片规则：
- 帖子/评论/回复：sha256(id) 十六进制摘要前 2 位（256 桶——c_/r_ 字面前缀分治，各 256 桶互不混合）。
- 媒体：文件名摘要前 2 位——文件名即哈希，无需二次哈希。

纯函数纪律：只构造/逆解析 Path 对象——零磁盘 IO、不创建目录（目录创建归 writer）。
fail loud：一切畸形 id / 畸形路径 raise PathFormatError，绝不返回 None 或静默回退。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PathFormatError",
    "ParsedEntity",
    "shard_of",
    "feed_dir",
    "feed_path",
    "comment_dir",
    "comment_path",
    "media_dir",
    "media_path",
    "resolve",
]

SHARD_LEN = 2

# MediaAsset.yaml Productions 逐字：media_file ::= /^[0-9a-f]{64}\.(jpg|png|mp4|gif)$/
_MEDIA_FILE_RE = re.compile(r"^[0-9a-f]{64}\.(jpg|png|mp4|gif)$")
_HEX2_RE = re.compile(r"^[0-9a-f]{2}$")
_SHARD_DIR_RE = re.compile(r"^([Bcr])_([0-9a-f]{2})$")

_FEED_PREFIX = "B_"
_COMMENT_KINDS = ("c", "r")


class PathFormatError(ValueError):
    """畸形 id / 畸形媒体文件名 / 畸形实体路径——契约语法违例，fail loud。"""


@dataclass(frozen=True)
class ParsedEntity:
    """resolve() 逆解析产物：kind + guild + shard + id（media 时 id = 文件名）。"""

    kind: str  # "feed" | "comment" | "reply" | "media"
    guild: str
    shard: str
    id: str


def shard_of(key: str) -> str:
    """sha256(key) 十六进制摘要前 2 位（256 桶；纯函数，无 IO）。"""
    if not isinstance(key, str) or not key:
        raise PathFormatError(f"shard key must be a non-empty string, got {key!r}")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:SHARD_LEN]


def _validate_id(prefix: str, entity_id: str) -> str:
    """id 必须以指定字面前缀开头且无路径成分——未知前缀 fail loud（非 None/静默）。"""
    if not isinstance(entity_id, str):
        raise PathFormatError(f"entity id must be str, got {type(entity_id).__name__}")
    if not entity_id.startswith(prefix) or len(entity_id) <= len(prefix):
        raise PathFormatError(
            f"entity id {entity_id!r} lacks required literal prefix {prefix!r}"
        )
    if "/" in entity_id or "\\" in entity_id or entity_id in (".", ".."):
        raise PathFormatError(f"entity id {entity_id!r} contains path separators")
    return entity_id


def _coerce_root(data_root: Path | str) -> Path:
    return Path(data_root)


def feed_dir(data_root: Path | str, guild: str, feed_id: str) -> Path:
    """data/{guild}/feeds/B_{feed_shard}/（feed_shard = sha256(feed_id)[:2]）。"""
    _validate_id(_FEED_PREFIX, feed_id)
    return _coerce_root(data_root) / guild / "feeds" / f"B_{shard_of(feed_id)}"


def feed_path(data_root: Path | str, guild: str, feed_id: str) -> Path:
    """data/{guild}/feeds/B_{feed_shard}/{feed_id}.json"""
    return feed_dir(data_root, guild, feed_id) / f"{feed_id}.json"


def comment_dir(data_root: Path | str, guild: str, comment_id: str) -> Path:
    """data/{guild}/comments/{c,r}_{shard}/——kind 由 id 字面前缀分治。"""
    if not isinstance(comment_id, str):
        raise PathFormatError(
            f"comment id must be str, got {type(comment_id).__name__}"
        )
    for kind in _COMMENT_KINDS:
        if comment_id.startswith(f"{kind}_"):
            _validate_id(f"{kind}_", comment_id)
            return _coerce_root(data_root) / guild / "comments" / f"{kind}_{shard_of(comment_id)}"
    raise PathFormatError(
        f"comment id {comment_id!r} lacks literal kind prefix (c_/r_); "
        f"feed ids (B_) belong in feed_path"
    )


def comment_path(data_root: Path | str, guild: str, comment_id: str) -> Path:
    """data/{guild}/comments/{c,r}_{shard}/{comment_id}.json（评论与回复同函数，前缀分治）。"""
    return comment_dir(data_root, guild, comment_id) / f"{comment_id}.json"


def media_dir(data_root: Path | str, guild: str, media_file: str) -> Path:
    """data/{guild}/media/{media_shard}/（media_shard = 摘要前 2 位，文件名即哈希）。"""
    if not _MEDIA_FILE_RE.match(media_file):
        raise PathFormatError(
            f"media file name {media_file!r} violates contract grammar "
            r"/^[0-9a-f]{64}\.(jpg|png|mp4)$/"
        )
    return _coerce_root(data_root) / guild / "media" / media_file[:SHARD_LEN]


def media_path(data_root: Path | str, guild: str, media_file: str) -> Path:
    """data/{guild}/media/{media_shard}/{media_file}"""
    return media_dir(data_root, guild, media_file) / media_file


def resolve(path: Path | str) -> ParsedEntity:
    """逆解析：实体/媒体路径 → 身份（kind/guild/shard/id）。畸形路径 fail loud。

    校验三重：目录字面（feeds/comments/media）、分片桶形态、分片与 id 的一致性
    （错桶文件 = 契约违例，同样 raise）。
    """
    parts = Path(path).parts
    if len(parts) < 4:
        raise PathFormatError(
            f"path {str(path)!r} too short to carry "
            "{guild}/{kind_dir}/{shard_dir}/{file} (contract Location templates)"
        )
    guild, kind_dir, shard_dir, fname = parts[-4], parts[-3], parts[-2], parts[-1]

    if kind_dir == "media":
        if not _HEX2_RE.match(shard_dir):
            raise PathFormatError(f"media shard dir {shard_dir!r} is not a 2-hex bucket")
        if not _MEDIA_FILE_RE.match(fname):
            raise PathFormatError(
                f"media file name {fname!r} violates contract grammar "
                r"/^[0-9a-f]{64}\.(jpg|png|mp4)$/"
            )
        if shard_dir != fname[:SHARD_LEN]:
            raise PathFormatError(
                f"media {fname!r} sits in bucket {shard_dir!r}, expected {fname[:SHARD_LEN]!r}"
            )
        return ParsedEntity(kind="media", guild=guild, shard=shard_dir, id=fname)

    dir_m = _SHARD_DIR_RE.match(shard_dir)
    if dir_m is None:
        raise PathFormatError(f"shard dir {shard_dir!r} is not a 2-hex bucket")

    if kind_dir == "feeds":
        letter, shard = dir_m.group(1), dir_m.group(2)
        if letter != "B":
            raise PathFormatError(f"feeds/ requires B_ shard dir, got {shard_dir!r}")
        if not fname.endswith(".json"):
            raise PathFormatError(f"feed file {fname!r} must end with .json")
        feed_id = _validate_id(_FEED_PREFIX, fname[: -len(".json")])
        if shard != shard_of(feed_id):
            raise PathFormatError(
                f"feed {feed_id!r} sits in bucket B_{shard}, expected B_{shard_of(feed_id)}"
            )
        return ParsedEntity(kind="feed", guild=guild, shard=shard, id=feed_id)

    if kind_dir == "comments":
        letter, shard = dir_m.group(1), dir_m.group(2)
        if letter not in _COMMENT_KINDS:
            raise PathFormatError(f"comments/ requires c_/r_ shard dir, got {shard_dir!r}")
        if not fname.endswith(".json"):
            raise PathFormatError(f"comment file {fname!r} must end with .json")
        entity_id = fname[: -len(".json")]
        expected_prefix = f"{letter}_"
        _validate_id(expected_prefix, entity_id)
        if shard != shard_of(entity_id):
            raise PathFormatError(
                f"comment {entity_id!r} sits in bucket {shard_dir!r}, "
                f"expected {letter}_{shard_of(entity_id)}"
            )
        kind = "comment" if letter == "c" else "reply"
        return ParsedEntity(kind=kind, guild=guild, shard=shard, id=entity_id)

    raise PathFormatError(
        f"unknown entity directory {kind_dir!r} (expected feeds/comments/media)"
    )
