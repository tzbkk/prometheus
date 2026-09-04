"""原子实体写者（契约唯一规格：structures/{Feed,Comment,Reply}.yaml）。

序列化纪律（写入规范，全体适用，逐字）：
UTF-8 无 BOM · indent=2 · ensure_ascii=False（中文直存）· 腾讯键序保留、_p 恒为末键 ·
.tmp + fsync + os.replace 原子替换。

_p 生命周期（可变性 = 整文件重写）：
- 重拉：顶层逐字替换（新载荷为准——旧顶层一字不带走，含旧独有键的消失）；
  first_seen 从旧 _p 保留（两本时钟之一，永不改写）；last_seen = now_ms（每次重写必更新）；
  media 按 url 合并——旧键不删（只填不删）、同 url 新 8 字段块整体替换、仅旧有条目按原序追加。
- 写前晋升（写前判，非事后扫）：条目 status=failed 且 retries>=3 → dead。
- backfill 路径（touch_clocks=False）：重写实体但两本时钟冻结（实体观测与媒体尝试是两本时钟）。

fail loud（严格解析，禁多路回退）：
载荷夹带 _p / id 不符 / media 条目缺 url / 存量文件损坏或缺 _p / backfill 无对象
→ WriterError，绝不静默跳过、绝不猜测修复。

纯 JSON 面：零腾讯解析（顶体由调用方逐字传入）、零网络 IO；目录创建经 paths 定位后
mkdir(parents=True, exist_ok=True)（目录创建归本模块）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.entity_store.paths import comment_path, feed_path

__all__ = [
    "WriterError",
    "write_entity",
    "DEAD_PROMOTION_RETRIES",
]

DEAD_PROMOTION_RETRIES = 3

_P_CLOCK_FIELDS = ("first_seen", "last_seen")


class WriterError(ValueError):
    """载荷/存量文件违反实体契约（夹带或缺失 _p、id 不符、畸形 media、backfill 无对象）——fail loud。"""


def write_entity(
    data_root: Path | str,
    guild: str,
    entity_id: str,
    body: dict,
    *,
    captured_via: str,
    media: list[dict],
    now_ms: int,
    feed_id: str | None = None,
    touch_clocks: bool = True,
) -> Path:
    """写实体（新见/重拉共用入口；backfill 走 touch_clocks=False）。

    - body：腾讯原体顶层逐字（**不得**含 _p——_p 是写者领地，夹带即 raise）；
      body["id"] 必须与 entity_id 一致（防错文件写）。
    - captured_via：捕获通道溯源（评论/回复亦必填）。
    - media：本次观测到的媒体 8 字段块清单（按 url 与存量合并）。
    - now_ms：观测时刻（unix 毫秒）——时钟由调用方注入（测试确定性惯例：mock 打最外层缝）。
    - feed_id：评论/回复必填（页上下文注入，Comment/Reply 契约 _p.feed_id）；
      feed 实体禁传（实体即文件名，Feed 契约 _p 无此项）。
    - touch_clocks=True：first_seen 首见取 now_ms/重拉保留，last_seen=now_ms；
      touch_clocks=False（backfill）：两本时钟冻结于存量值，要求文件已存在。

    返回落盘路径。序列化纪律见模块 docstring；写前晋升 failed→dead@retries>=3。
    """
    is_comment = entity_id.startswith(("c_", "r_"))
    _validate_payload(
        entity_id, body, captured_via, media, now_ms, feed_id, is_comment, guild
    )

    path = _entity_path(data_root, guild, entity_id)
    old_p = _load_existing_p(path, entity_id) if path.exists() else None

    if touch_clocks:
        first_seen = old_p["first_seen"] if old_p is not None else now_ms
        last_seen = now_ms
    else:
        if old_p is None:
            raise WriterError(
                f"backfill (touch_clocks=False) requires an existing entity: {path}"
            )
        first_seen = old_p["first_seen"]
        last_seen = old_p["last_seen"]

    merged_media = _merge_media(old_p["media"] if old_p is not None else [], media)
    _promote_failed_to_dead(merged_media)

    # _p 键序 = 契约表序（Feed: captured_via..media；Comment/Reply 多首键 feed_id）
    p: dict = {}
    if is_comment:
        p["feed_id"] = feed_id
    p["captured_via"] = captured_via
    p["first_seen"] = first_seen
    p["last_seen"] = last_seen
    p["media"] = merged_media

    _atomic_write(path, {**body, "_p": p})  # {**body} 保腾讯键序，_p 恒末键
    return path


def _entity_path(data_root: Path | str, guild: str, entity_id: str) -> Path:
    """B_ → feed_path；c_/r_ → comment_path（前缀分治）；未知前缀由 paths fail loud。"""
    if entity_id.startswith("B_"):
        return feed_path(data_root, guild, entity_id)
    return comment_path(data_root, guild, entity_id)


def _validate_payload(
    entity_id: str,
    body: dict,
    captured_via: str,
    media: list[dict],
    now_ms: int,
    feed_id: str | None,
    is_comment: bool,
    guild: str,
) -> None:
    if not isinstance(body, dict):
        raise WriterError(f"payload body must be a JSON object, got {type(body).__name__}")
    if "_p" in body:
        raise WriterError(
            "payload body must not carry the _p namespace (_p is writer-owned; "
            "smuggling it through the top level breaks key-order discipline)"
        )
    if body.get("id") != entity_id:
        raise WriterError(
            f"payload id {body.get('id')!r} does not match entity id {entity_id!r}"
        )
    if not isinstance(captured_via, str) or not captured_via:
        raise WriterError(f"captured_via must be a non-empty string, got {captured_via!r}")
    if type(now_ms) is not int:
        raise WriterError(f"now_ms must be int (unix ms), got {type(now_ms).__name__}")
    if is_comment and feed_id is None:
        raise WriterError(
            f"comment/reply entity {entity_id!r} requires feed_id (page-context injection)"
        )
    if not is_comment and feed_id is not None:
        raise WriterError(
            f"feed entity {entity_id!r} takes no feed_id (entity id IS the file name)"
        )
    if entity_id.startswith("B_"):
        # 防错树写：feed 顶体自带 guild 归属（契约 channelInfo.sign.guild_id）
        gid = ((body.get("channelInfo") or {}).get("sign") or {}).get("guild_id")
        if gid is not None and gid != str(guild):
            raise WriterError(
                f"feed payload belongs to guild {gid!r} but is being written under {guild!r}"
            )
    _validate_media_list(media, "incoming media list")


def _validate_media_list(media: object, where: str) -> None:
    """media 清单结构判：list[dict] 且每条含非空 str url（合并身份键，缺即无以合并）。"""
    if not isinstance(media, list):
        raise WriterError(f"{where} must be a list, got {type(media).__name__}")
    for index, entry in enumerate(media):
        if not isinstance(entry, dict):
            raise WriterError(f"{where}[{index}] must be an object, got {type(entry).__name__}")
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise WriterError(
                f"{where}[{index}] lacks a non-empty string 'url' "
                "(media identity key for merge/dead-set)"
            )


def _load_existing_p(path: Path, entity_id: str) -> dict:
    """读存量实体的 _p（合并基准）。损坏/缺 _p/id 不符 → WriterError（禁多路回退解析）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise WriterError(f"existing entity {path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise WriterError(f"existing entity {path} is not a JSON object")
    if raw.get("id") != entity_id:
        raise WriterError(
            f"existing entity {path} carries id {raw.get('id')!r}, expected {entity_id!r}"
        )
    p = raw.get("_p")
    if not isinstance(p, dict):
        raise WriterError(
            f"existing entity {path} lacks the _p namespace "
            "(contract violation — no fallback parsing, fail loud at boundary)"
        )
    for key in _P_CLOCK_FIELDS:
        if type(p.get(key)) is not int:
            raise WriterError(
                f"existing entity {path} has non-integer _p.{key}: {p.get(key)!r}"
            )
    _validate_media_list(p.get("media"), f"existing _p.media of {path}")
    return p


def _merge_media(old: list[dict], new: list[dict]) -> list[dict]:
    """合并语义（media 只填不删）：

    - 新清单序在前（本次载荷的腾讯序为准）；
    - 同 url → 新 8 字段块整体替换该媒体条目（同键新状态覆盖）；
    - 仅旧有条目按原序追加（旧键不删）。
    条目浅拷贝：晋升改写不波及调用方对象。
    """
    new_urls = {entry["url"] for entry in new}
    merged = [dict(entry) for entry in new]
    merged.extend(dict(entry) for entry in old if entry["url"] not in new_urls)
    return merged


def _promote_failed_to_dead(media: list[dict]) -> None:
    """写前晋升（写前判，非事后扫）：status=failed 且 retries>=DEAD_PROMOTION_RETRIES → dead。"""
    for entry in media:
        if entry.get("status") == "failed" and entry.get("retries", 0) >= DEAD_PROMOTION_RETRIES:
            entry["status"] = "dead"


def _atomic_write(path: Path, doc: dict) -> None:
    """原子写：.tmp + fsync + os.replace；UTF-8 无 BOM / indent=2 / ensure_ascii=False。

    tmp 命名 = 同目录 path.with_suffix('.tmp')（lock.py 同款惯例）；
    崩溃残留 .tmp 可接受（下一轮覆写），最终文件要么旧字节要么新字节、绝无撕裂。
    """
    payload = json.dumps(doc, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
