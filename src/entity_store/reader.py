"""读取投影库——投影的唯一合法居所（消费者强制 import 此处）。

公开 API（函数名/签名/语义逐字，一字不改）：
    kind_of(doc)        -> "comment" | "reply"      # id 前缀判定
    text_of(doc)        -> str                      # richContents.contents[] 遍历拼接
    author_of(doc)      -> {id, nick, avatar_url}   # postUser 投影
    created_at_of(doc)  -> int                      # int(createTime)，内存中转换
    media_of(doc)       -> _p.media
    target_of(doc)      -> {reply_id, user_id, nick} | None

（分片助手 shard 见 paths.shard_of——桶定位归路径库，非文档投影。）

feed 侧双投影（与六投影并列同款纪律——腾讯双形状实证：
feed 富文本居 title.contents、作者居 poster，评论居 richContents/postUser，
doc/DATA_FORMAT.md §feeds.jsonl）：
    title_of(doc)       -> str | None                 # title.contents[] 遍历拼接
    poster_of(doc)      -> {id, nick, avatar_url} | None  # poster 投影

实体文件 = 腾讯原体（顶层一字不动）+ _p 命名空间；投影是原体的
唯一合法读取视图——零写盘、零网络 IO、零腾讯解析以外的格式猜测；本模块外不得出现
任何投影逻辑。

严格加载（严格解析，禁多路回退）：load_entity 读出的文档缺 _p /
JSON 损坏 / 非 JSON 对象 → ReaderError，绝不静默回退；投影函数作用于已加载 dict，
对各自消费的切片同样 fail loud（缺 postUser / 畸形 createTime / 缺 _p.media）。

投影纯度：一切投影函数不 mutate 文档（text_of/author_of/target_of 构造新对象；
media_of 按签名返回 _p.media 列表本体——消费者不得改写，改写归 writer 领地）。
腾讯 verbatim 面的字段存在性差异（targetUser/targetReplyID 可缺）按签名以
None/空投影表达，不做猜测修复。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = [
    "ReaderError",
    "load_entity",
    "kind_of",
    "text_of",
    "author_of",
    "created_at_of",
    "media_of",
    "target_of",
    "title_of",
    "poster_of",
]

_COMMENT_KINDS = {"c": "comment", "r": "reply"}


class ReaderError(ValueError):
    """文档违反实体契约（缺 _p / JSON 损坏 / 投影切片畸形）——fail loud，无静默回退。"""


def load_entity(path: Path | str) -> dict:
    """严格加载实体文件（磁盘 → 已加载 dict；投影函数的正规入口）。

    JSON 损坏 / 顶层非对象 / 缺 _p 命名空间 → ReaderError（写者纪律的读侧镜像：
    范式 B 下每个实体文件必有 _p，缺即损坏或异构文件，fail loud）。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ReaderError(f"entity file {path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReaderError(f"entity file {path} is not a JSON object")
    if not isinstance(raw.get("_p"), dict):
        raise ReaderError(
            f"entity file {path} lacks the _p namespace "
            "(contract violation — no fallback parsing, fail loud at boundary)"
        )
    return raw


def _comment_id(doc: dict) -> str:
    """评论/回复 id（kind 前缀判定共用底座）；缺 id / 未知前缀 → ReaderError。"""
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    entity_id = doc.get("id")
    if not isinstance(entity_id, str) or not entity_id.startswith(("c_", "r_")):
        raise ReaderError(
            f"entity id {entity_id!r} lacks literal kind prefix (c_/r_); "
            "feed entities (B_) are not comment-tree projections"
        )
    return entity_id


def kind_of(doc: dict) -> str:
    """"comment" | "reply"——id 字面前缀判定。"""
    return _COMMENT_KINDS[_comment_id(doc)[0]]


def text_of(doc: dict) -> str:
    """richContents.contents[] 遍历拼接——取各段 text_content.text，空格连接。

    无文本段（contents 空 / richContents 缺）投影为 ""——空文本是合法投影，
    非契约违例；richContents 存在但非对象 / contents 非数组 → ReaderError。
    """
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    rich = doc.get("richContents")
    if rich is None:
        return ""
    if not isinstance(rich, dict):
        raise ReaderError(f"richContents must be an object, got {type(rich).__name__}")
    contents = rich.get("contents")
    if contents is None:
        return ""
    if not isinstance(contents, list):
        raise ReaderError(f"richContents.contents must be an array, got {type(contents).__name__}")
    parts: list[str] = []
    for index, entry in enumerate(contents):
        if not isinstance(entry, dict):
            raise ReaderError(
                f"richContents.contents[{index}] must be an object, got {type(entry).__name__}"
            )
        text_content = entry.get("text_content")
        if isinstance(text_content, dict):
            text = text_content.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return " ".join(parts)


def author_of(doc: dict) -> dict:
    """postUser 投影 → {id, nick, avatar_url}（头像走 postUser.icon.iconUrl）。"""
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    post_user = doc.get("postUser")
    if not isinstance(post_user, dict):
        raise ReaderError(
            f"postUser must be an object, got {type(post_user).__name__} "
            "(comment/reply without postUser is contract garbage)"
        )
    icon = post_user.get("icon")
    avatar_url = icon.get("iconUrl") if isinstance(icon, dict) else None
    return {
        "id": post_user.get("id"),
        "nick": post_user.get("nick"),
        "avatar_url": avatar_url,
    }


def created_at_of(doc: dict) -> int:
    """int(createTime) 内存转换——腾讯十进制字符串秒，返回 int，不落盘。"""
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    raw = doc.get("createTime")
    if raw is None or isinstance(raw, bool):
        raise ReaderError(f"createTime {raw!r} is not a decimal timestamp")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ReaderError(f"createTime {raw!r} is not a decimal timestamp: {exc}") from exc


def media_of(doc: dict) -> list:
    """_p.media 引用清单（返回 _p.media 列表本体）。

    列表本体按引用返回（消费者只读——媒体状态机改写归 writer 领地）；
    缺 _p（未严格加载）或 _p.media 非数组 → ReaderError。
    """
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    p = doc.get("_p")
    if not isinstance(p, dict):
        raise ReaderError(
            "document lacks the _p namespace (load via load_entity — "
            "projections require the strict-loaded contract shape)"
        )
    media = p.get("media")
    if not isinstance(media, list):
        raise ReaderError(f"_p.media must be an array, got {type(media).__name__}")
    return media


def target_of(doc: dict) -> dict | None:
    """回复定位投影 → {reply_id, user_id, nick} | None。

    消费腾讯 verbatim 面的 targetReplyID / targetUser（Reply 契约 Description：
    定位语义归目标回复，契约不钉）。两者皆缺 → None（顶层评论/对主楼回复）；
    部分在场按所得投影（缺席槽位 None，不猜测）。targetUser 在场但非对象 → ReaderError。
    """
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    reply_id = doc.get("targetReplyID")
    target_user = doc.get("targetUser")
    if not reply_id and not target_user:
        return None
    if target_user is not None and not isinstance(target_user, dict):
        raise ReaderError(
            f"targetUser must be an object, got {type(target_user).__name__}"
        )
    return {
        "reply_id": reply_id or None,
        "user_id": target_user.get("id") if target_user else None,
        "nick": target_user.get("nick") if target_user else None,
    }


def title_of(doc: dict) -> str | None:
    """feed 标题投影 → title.contents[] 遍历拼接。

    feed 的富文本居 title.contents（评论居 richContents.contents——text_of）；
    非空 text_content.text 段空格连接与 text_of 一致。无 title / 无文本段 → None
    （FeedList.title_text 可空语义——无标题帖与"标题为空串"有别）。
    title 在场但非对象 / contents 非数组 / 条目非对象 → ReaderError（结构性容器
    畸形 fail loud；值缺席归 None，同款分界）。
    """
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    return _rich_text_join(doc.get("title"), "title")


def body_of(doc: dict) -> str | None:
    """feed 正文投影 → 顶层 contents.contents[] 遍历拼接（全文）。

    腾讯载荷双富文本信封：title 是列表预览（服务端截断 ~29 字），顶层
    contents 是完整正文（同构遍历）。缺 contents / 无文本段 → None；
    容器畸形 → ReaderError（与 title_of 同款分界）。
    """
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    return _rich_text_join(doc.get("contents"), "contents")


def _rich_text_join(envelope: object, name: str) -> str | None:
    if envelope is None:
        return None
    if not isinstance(envelope, dict):
        raise ReaderError(f"{name} must be an object, got {type(envelope).__name__}")
    contents = envelope.get("contents")
    if contents is None:
        return None
    if not isinstance(contents, list):
        raise ReaderError(f"{name}.contents must be an array, got {type(contents).__name__}")
    parts: list[str] = []
    for index, entry in enumerate(contents):
        if not isinstance(entry, dict):
            raise ReaderError(
                f"{name}.contents[{index}] must be an object, got {type(entry).__name__}"
            )
        text_content = entry.get("text_content")
        if isinstance(text_content, dict):
            text = text_content.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    if not parts:
        return None
    return " ".join(parts)


def poster_of(doc: dict) -> dict | None:
    """feed 作者投影 → {id, nick, avatar_url} | None（poster 命名空间，author_of 的 feed 对偶）。

    poster 整缺（无作者的最小载荷）→ None；部分在场 → 缺席槽位 None（不猜测）；
    poster 在场但非对象 → ReaderError。头像走 poster.icon.iconUrl（与 author_of 的
    postUser.icon.iconUrl 同构）。
    """
    if not isinstance(doc, dict):
        raise ReaderError(f"document must be a JSON object, got {type(doc).__name__}")
    poster = doc.get("poster")
    if poster is None:
        return None
    if not isinstance(poster, dict):
        raise ReaderError(f"poster must be an object, got {type(poster).__name__}")
    icon = poster.get("icon")
    avatar_url = icon.get("iconUrl") if isinstance(icon, dict) else None
    return {
        "id": poster.get("id"),
        "nick": poster.get("nick"),
        "avatar_url": avatar_url,
    }
