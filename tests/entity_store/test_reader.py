"""读取投影库 mandate 测试（MD-023 ~ MD-026，pillar 对应面）。

契约溯源： API 块（六投影函数名/签名/语义逐字）+ 原则 4（严格解析 fail loud）
+ Comment/Reply 契约（_p.feed_id 归属链、vecReply 克隆 verbatim 双存、createTime
十进制字符串秒）。全部合成腾讯形载荷；落盘经 writer（边界往返：写→严格加载→投影）。
"""

from __future__ import annotations

import copy
import json

import pytest

from src.entity_store import (
    ReaderError,
    author_of,
    comment_path,
    created_at_of,
    feed_path,
    kind_of,
    load_entity,
    media_of,
    poster_of,
    target_of,
    text_of,
    body_of,
    title_of,
    write_entity,
)

GUILD = "1000000000000001"
FEED_ID = "B_9d8c7b6a5f4e3d2c1b0a9988776655443322110f"
FEED_ID_T52 = "B_5f2e8a3b9c1d4e6f7a8b9c0d1e2f3a4b5c6d7e"
COMMENT_ID = "c_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"
REPLY_ID = "r_0f9e8d7c6b5a4938271605f4e3d2c1b0a99887"
CLONE_REPLY_ID = "r_feedeadbeeffeedeadbeeffeedeadbeeffeedead"

T1 = 1782919600000

URL_A = "https://channel.photo.store.qq.com/a.png"


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


def comment_body(comment_id: str = COMMENT_ID, **overrides) -> dict:
    """合成腾讯评论顶体（camelCase 原样键 + 中文值；createTime 契约实证 = 字符串秒）。"""
    body = {
        "id": comment_id,
        "createTime": "1782920526",
        "postUser": {
            "id": "u_2",
            "nick": "评论者",
            "icon": {"iconUrl": "https://thirdqq.qq.com/avatar_u2.jpg"},
        },
        "richContents": {
            "contents": [
                {"text_content": {"text": "中文第一段"}},
                {"text_content": {"text": "中文第二段"}},
            ],
            "ip_location_province": "浙江",
        },
        "likeInfo": {"count": "3"},
        "replyCount": 1,
    }
    body.update(overrides)
    return body


def write_comment(data_root, body: dict, media: list | None = None):
    return write_entity(
        data_root, GUILD, body["id"], body,
        captured_via="scraper", media=media or [], now_ms=T1, feed_id=FEED_ID,
    )


def test_reader_projections_project_tencent_fields(data_root):
    """MD-023：六投影正例—— 形态逐字（含 int(createTime) 内存转换与 target None 分支）。"""
    write_comment(data_root, comment_body(), media=[media_entry(URL_A)])
    write_entity(
        data_root, GUILD, REPLY_ID, comment_body(
            REPLY_ID,
            targetReplyID=COMMENT_ID,
            targetUser={"id": "u_9", "nick": "被回复者"},
        ),
        captured_via="scraper", media=[], now_ms=T1, feed_id=FEED_ID,
    )

    comment = load_entity(comment_path(data_root, GUILD, COMMENT_ID))
    reply = load_entity(comment_path(data_root, GUILD, REPLY_ID))

    assert kind_of(comment) == "comment"
    assert kind_of(reply) == "reply"

    assert text_of(comment) == "中文第一段 中文第二段"  # contents[] 遍历拼接
    assert text_of(comment_body(richContents={"contents": []})) == ""
    assert text_of(comment_body(richContents=None)) == ""

    assert author_of(comment) == {
        "id": "u_2",
        "nick": "评论者",
        "avatar_url": "https://thirdqq.qq.com/avatar_u2.jpg",
    }
    assert author_of(comment_body(postUser={"id": "u_3", "nick": "无头像者"})) == {
        "id": "u_3",
        "nick": "无头像者",
        "avatar_url": None,
    }

    created = created_at_of(comment)
    assert type(created) is int and created == 1782920526  # 字符串秒 → int，内存转换

    assert media_of(comment) == [media_entry(URL_A)]  # _p.media 逐条原样

    assert target_of(reply) == {"reply_id": COMMENT_ID, "user_id": "u_9", "nick": "被回复者"}
    assert target_of(comment) is None  # 顶层评论无定位目标


def test_reader_vecreply_clone_projects_as_reply(data_root):
    """MD-024：vecReply 克隆用例——嵌套回复逐字保留（verbatim 不剥离），克隆按 reply 投影。"""
    clone = {
        "id": CLONE_REPLY_ID,
        "createTime": "1782920600",
        "postUser": {"id": "u_5", "nick": "楼中楼者", "icon": {"iconUrl": "https://thirdqq.qq.com/avatar_u5.jpg"}},
        "richContents": {"contents": [{"text_content": {"text": "楼中楼克隆文本"}}]},
        "targetReplyID": COMMENT_ID,
        "targetUser": {"id": "u_2", "nick": "评论者"},
    }
    write_comment(data_root, comment_body(vecReply=[clone]))

    doc = load_entity(comment_path(data_root, GUILD, COMMENT_ID))
    assert doc["vecReply"] == [clone]  # 克隆逐字存活于顶层（冗余双存，）
    nested = doc["vecReply"][0]

    assert kind_of(nested) == "reply"  # r_ 前缀判定穿透嵌套克隆
    assert text_of(nested) == "楼中楼克隆文本"
    assert author_of(nested) == {
        "id": "u_5",
        "nick": "楼中楼者",
        "avatar_url": "https://thirdqq.qq.com/avatar_u5.jpg",
    }
    assert created_at_of(nested) == 1782920600
    assert target_of(nested) == {"reply_id": COMMENT_ID, "user_id": "u_2", "nick": "评论者"}

    assert kind_of(doc) == "comment"  # 父文档不受克隆投影影响
    assert media_of(doc) == []  # media 是文件级 _p 命名空间
    with pytest.raises(ReaderError):
        media_of(nested)  # 克隆是 verbatim 元素，无 _p——媒体投影拒绝作用于它


def test_reader_projections_leave_document_unmutated(data_root):
    """MD-025：投影纯度——全六函数调用前后文档字节/深度相等；新建对象改写不波及原文档。"""
    body = comment_body()
    write_comment(data_root, body, media=[media_entry(URL_A)])
    path = comment_path(data_root, GUILD, COMMENT_ID)
    doc = load_entity(path)
    snapshot = copy.deepcopy(doc)
    raw_bytes = path.read_bytes()

    kind_of(doc)
    text_of(doc)
    projected_author = author_of(doc)
    created_at_of(doc)
    projected_media = media_of(doc)
    target_of(doc)
    load_entity(path)

    assert path.read_bytes() == raw_bytes  # 字节级不变
    assert doc == snapshot  # 深度相等

    projected_author["nick"] = "投影侧篡改"  # author_of 产物是新建 dict
    assert doc["postUser"]["nick"] == "评论者"

    assert projected_media is doc["_p"]["media"]  # 投影签名逐字：返回 _p.media 列表本体（消费者只读约定）


def test_reader_strict_load_fails_loud_on_missing_p(data_root, tmp_path):
    """MD-026：负例——缺 _p/JSON 损坏/非对象/投影切片畸形一律 ReaderError（无多路回退）。"""
    bare = comment_body()
    bare_path = tmp_path / "bare.json"
    bare_path.write_text(json.dumps(bare, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ReaderError):
        load_entity(bare_path)  # 无 _p 文档 → raise（任务 QA 负例原文）

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ReaderError):
        load_entity(corrupt)

    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ReaderError):
        load_entity(array_path)

    with pytest.raises(ReaderError):
        media_of(bare)  # 未严格加载（缺 _p）→ 媒体投影拒绝

    with pytest.raises(ReaderError):
        kind_of(comment_body(id="x_bad_prefix"))  # 未知前缀 fail loud（非 None/静默）
    with pytest.raises(ReaderError):
        kind_of({"id": 42})
    with pytest.raises(ReaderError):
        author_of(comment_body(postUser=None))
    with pytest.raises(ReaderError):
        created_at_of(comment_body(createTime="不是时间戳"))
    with pytest.raises(ReaderError):
        created_at_of(comment_body(createTime=None))
    with pytest.raises(ReaderError):
        text_of(comment_body(richContents="不是对象"))
    with pytest.raises(ReaderError):
        target_of(comment_body(targetUser="不是对象"))


def test_reader_feed_projections_title_and_poster(data_root):
    """MD-052：feed 侧双投影——title.contents 拼接与 poster
    三键；无 title/poster → None（FeedList 可空语义）；畸形容器 ReaderError；不 mutate。"""
    full = {
        "id": FEED_ID_T52,
        "createTime": "1782919600",
        "channelInfo": {"sign": {"guild_id": GUILD}},
        "title": {"contents": [
            {"text_content": {"text": "标题一"}},
            {"plain": "无文本段不投影"},
            {"text_content": {"text": "标题二"}},
        ]},
        "poster": {
            "id": "u_1",
            "nick": "作者",
            "icon": {"iconUrl": "https://thirdqq.qq.com/a.jpg"},
        },
    }
    write_entity(
        data_root, GUILD, FEED_ID_T52, full,
        captured_via="scraper", media=[], now_ms=T1,
    )
    doc = load_entity(feed_path(data_root, GUILD, FEED_ID_T52))
    snapshot = copy.deepcopy(doc)

    assert title_of(doc) == "标题一 标题二"  # 非空段空格连接（与 text_of 一致）
    assert poster_of(doc) == {
        "id": "u_1",
        "nick": "作者",
        "avatar_url": "https://thirdqq.qq.com/a.jpg",
    }

    def body(**overrides):
        merged = dict(full)
        merged.update(overrides)
        return merged

    # poster 部分在场 → 缺席槽位 None（不猜测）；icon 缺 → avatar None
    assert poster_of(body(poster={"id": "u_2"})) == {
        "id": "u_2", "nick": None, "avatar_url": None,
    }

    # 无 title / title 无 contents / 无文本段 → None（无标题帖 ≠ 空串标题）
    bare = {
        "id": FEED_ID_T52, "createTime": "1782919600",
        "channelInfo": {"sign": {"guild_id": GUILD}},
    }
    assert title_of(bare) is None
    assert poster_of(bare) is None
    assert title_of(body(title={})) is None
    assert title_of(body(title={"contents": []})) is None
    assert title_of(body(title={"contents": [{"text_content": {"text": ""}}]})) is None

    # 畸形容器 fail loud（结构性容器归 ReaderError；值缺席归 None——同款分界）
    with pytest.raises(ReaderError):
        title_of(body(title="不是对象"))
    with pytest.raises(ReaderError):
        title_of(body(title={"contents": "不是数组"}))
    with pytest.raises(ReaderError):
        title_of(body(title={"contents": ["不是对象"]}))
    with pytest.raises(ReaderError):
        poster_of(body(poster="不是对象"))

    # 投影纯度：不 mutate；poster_of 产物新建对象
    title_of(doc)
    projected = poster_of(doc)
    projected["nick"] = "投影侧篡改"
    assert doc == snapshot
    assert doc["poster"]["nick"] == "作者"


def test_reader_body_of_full_text_projection():
    """MD-138：正文投影——顶层 contents.contents 全文拼接（title 只是
    预览截断，两信封独立）；缺 contents/无文本段 → None；畸形容器 ReaderError。"""
    doc = {
        "id": FEED_ID,
        "createTime": "1782919600",
        "title": {"contents": [{"text_content": {"text": "预览截断的标题"}}]},
        "contents": {"contents": [
            {"text_content": {"text": "完整正文第一段"}},
            {"text_content": {"text": "\n"}},
            {"text_content": {"text": "完整正文第二段"}},
            {"text_content": None},
        ]},
    }
    assert body_of(doc) == "完整正文第一段 \n 完整正文第二段"
    assert title_of(doc) == "预览截断的标题"

    assert body_of({"id": FEED_ID}) is None
    assert body_of({"id": FEED_ID, "contents": {}}) is None
    assert body_of({"id": FEED_ID, "contents": {"contents": []}}) is None
    assert body_of(
        {"id": FEED_ID, "contents": {"contents": [{"text_content": {"text": ""}}]}}
    ) is None

    with pytest.raises(ReaderError):
        body_of({"id": FEED_ID, "contents": "不是对象"})
    with pytest.raises(ReaderError):
        body_of({"id": FEED_ID, "contents": {"contents": "不是数组"}})
    with pytest.raises(ReaderError):
        body_of({"id": FEED_ID, "contents": {"contents": ["不是对象"]}})
