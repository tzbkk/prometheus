"""原子实体写者 mandate 测试（MD-016 ~ MD-022，pillar 对应面+B）。

契约溯源：structures/{Feed,Comment,Reply}.yaml（_p 全钉 + Description 晋升规则）+
/§4.1/§4.2（可变性、两本时钟、media 只填不删）+ §四（写入规范序列化纪律）。
全部合成腾讯形载荷 + tmp_path 数据根；时钟经 now_ms 参数注入（外层缝惯例）。
"""

from __future__ import annotations

import json
import os

import pytest
from jsonschema import Draft202012Validator

from src.entity_store import (
    PathFormatError,
    WriterError,
    feed_path,
    shard_of,
    write_entity,
)

GUILD = "1000000000000001"
FEED_ID = "B_9d8c7b6a5f4e3d2c1b0a9988776655443322110f"
COMMENT_ID = "c_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"
REPLY_ID = "r_0f9e8d7c6b5a4938271605f4e3d2c1b0a99887"

T1 = 1782919600000
T2 = 1782920700000

URL_A = "https://channel.photo.store.qq.com/a.png"
URL_B = "https://channel.photo.store.qq.com/b.png"
URL_C = "https://channel.photo.store.qq.com/c.png"


def media_entry(url: str, **overrides) -> dict:
    """8 字段块全钉形态，overrides 点改单字段。"""
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


def feed_body(**overrides) -> dict:
    """合成腾讯 feed 顶体：键序刻意非字母序（键序保持断言用）+ 中文值（直存断言用）。"""
    body = {
        "id": FEED_ID,
        "createTime": "1782919600",
        "title": "中文标题——直存断言",
        "channelInfo": {"sign": {"guild_id": GUILD}},
        "postUser": {"id": "u_1", "nick": "作者昵称"},
        "feedAttchInfo": {"cursor": "abc"},
    }
    body.update(overrides)
    return body


def comment_body(comment_id: str = COMMENT_ID, **overrides) -> dict:
    body = {
        "id": comment_id,
        "content": "合成中文评论",
        "createTime": "1782920526",
        "postUser": {"id": "u_2", "nick": "评论者"},
    }
    body.update(overrides)
    return body


def written(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validator(compiled_schemas, name):
    return Draft202012Validator(compiled_schemas[name])


def test_writer_output_passes_compiled_schemas(data_root, compiled_schemas):
    fp = write_entity(
        data_root, GUILD, FEED_ID, feed_body(),
        captured_via="scraper", media=[media_entry(URL_A)], now_ms=T1,
    )
    cp = write_entity(
        data_root, GUILD, COMMENT_ID, comment_body(),
        captured_via="scraper", media=[], now_ms=T1, feed_id=FEED_ID,
    )
    rp = write_entity(
        data_root, GUILD, REPLY_ID, comment_body(REPLY_ID, content="合成中文回复"),
        captured_via="scraper", media=[], now_ms=T1, feed_id=FEED_ID,
    )
    assert fp == feed_path(data_root, GUILD, FEED_ID)
    assert cp.parent.name == f"c_{shard_of(COMMENT_ID)}"
    assert rp.parent.name == f"r_{shard_of(REPLY_ID)}"

    validator(compiled_schemas, "Feed").validate(written(fp))
    validator(compiled_schemas, "Comment").validate(written(cp))
    validator(compiled_schemas, "Reply").validate(written(rp))

    doc = written(fp)
    doc["_p"]["media"][0]["status"] = "deas"  # ：手误枚举必须被契约拒收
    with pytest.raises(Exception, match="deas|enum"):
        validator(compiled_schemas, "Feed").validate(doc)

    bad_file = dict(doc)
    bad_file["_p"] = dict(doc["_p"], media=[media_entry(URL_A, file="deadbeef.jpg")])
    with pytest.raises(Exception):
        validator(compiled_schemas, "Feed").validate(bad_file)


def test_writer_serialization_discipline_bytes(data_root):
    body = feed_body()
    fp = write_entity(
        data_root, GUILD, FEED_ID, body,
        captured_via="scraper", media=[media_entry(URL_A)], now_ms=T1,
    )
    raw = fp.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # 无 BOM
    text = raw.decode("utf-8")
    assert "中文标题——直存断言" in text  # ensure_ascii=False：中文直存，非 \u 转义
    assert '"title": "中文标题——直存断言"' in text and '\\"' not in text.split('"title"')[1][:40]

    doc = written(fp)
    assert list(doc) == list(body) + ["_p"]  # 腾讯键序保持 + _p 恒末键
    assert list(doc["_p"]) == ["captured_via", "first_seen", "last_seen", "media"]

    expected = {
        **body,
        "_p": {
            "captured_via": "scraper",
            "first_seen": T1,
            "last_seen": T1,
            "media": [media_entry(URL_A)],
        },
    }
    assert raw == json.dumps(expected, indent=2, ensure_ascii=False).encode("utf-8")

    cp = write_entity(
        data_root, GUILD, COMMENT_ID, comment_body(),
        captured_via="scraper", media=[], now_ms=T1, feed_id=FEED_ID,
    )
    assert list(written(cp)["_p"]) == ["feed_id", "captured_via", "first_seen", "last_seen", "media"]


def test_writer_repull_merges_p_lifecycle(data_root):
    write_entity(
        data_root, GUILD, FEED_ID,
        feed_body(feedAttchInfo={"cursor": "old"}, summary="旧顶层独有键"),
        captured_via="scraper",
        media=[media_entry(URL_A), media_entry(URL_B, type="video")],
        now_ms=T1,
    )
    new_body = feed_body(title="重拉后的新标题", feedAttchInfo={"cursor": "new"})
    write_entity(
        data_root, GUILD, FEED_ID, new_body,
        captured_via="scraper",
        media=[
            media_entry(URL_A, file="ab" + "0" * 62 + ".jpg", status="ok"),
            media_entry(URL_C),
        ],
        now_ms=T2,
    )

    doc = written(feed_path(data_root, GUILD, FEED_ID))
    assert doc["title"] == "重拉后的新标题"
    assert doc["feedAttchInfo"] == {"cursor": "new"}
    assert "summary" not in doc  # 顶层逐字替换：旧独有键不存活
    assert list(doc) == list(new_body) + ["_p"]  # 新载荷键序为准

    p = doc["_p"]
    assert p["first_seen"] == T1  # 两本时钟：first_seen 保留
    assert p["last_seen"] == T2  # last_seen = now
    assert [m["url"] for m in p["media"]] == [URL_A, URL_C, URL_B]  # 只填不删 + 新序在前
    assert p["media"][0]["status"] == "ok" and p["media"][0]["file"] is not None
    assert p["media"][2]["type"] == "video"  # 仅旧有条目原块原序追加


def test_writer_promotes_failed_media_to_dead_at_three_retries(data_root):
    media = [
        media_entry(URL_A, status="failed", retries=3, last_attempt_ts=T2),
        media_entry(URL_B, status="failed", retries=2, last_attempt_ts=T2),
        media_entry(URL_C, status="pending", retries=5),
    ]
    fp = write_entity(
        data_root, GUILD, FEED_ID, feed_body(),
        captured_via="scraper", media=media, now_ms=T1,
    )
    statuses = {m["url"]: m["status"] for m in written(fp)["_p"]["media"]}
    assert statuses == {URL_A: "dead", URL_B: "failed", URL_C: "pending"}

    write_entity(
        data_root, GUILD, FEED_ID, feed_body(),
        captured_via="scraper",
        media=[media_entry(URL_B, status="failed", retries=3, last_attempt_ts=T2)],
        now_ms=T2,
    )
    statuses = {m["url"]: m["status"] for m in written(fp)["_p"]["media"]}
    assert statuses[URL_B] == "dead"  # 重拉合并路径同样写前晋升


def test_writer_backfill_freezes_both_clocks(data_root):
    fp = write_entity(
        data_root, GUILD, FEED_ID, feed_body(),
        captured_via="scraper", media=[media_entry(URL_A)], now_ms=T1,
    )
    write_entity(
        data_root, GUILD, FEED_ID, feed_body(),
        captured_via="scraper",
        media=[
            media_entry(
                URL_A, file="cd" + "0" * 62 + ".png",
                status="ok", width=640, height=480, retries=1, last_attempt_ts=T2,
            )
        ],
        now_ms=T2,
        touch_clocks=False,
    )
    p = written(fp)["_p"]
    assert p["first_seen"] == T1  # 两本时钟冻结：backfill 不动 first_seen
    assert p["last_seen"] == T1  # 也不动 last_seen（媒体尝试是另一本时钟）
    assert p["media"][0]["file"] == "cd" + "0" * 62 + ".png"
    assert p["media"][0]["status"] == "ok"
    assert p["media"][0]["last_attempt_ts"] == T2

    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, "B_ff0000000000000000000000000000000000ff",
            feed_body(id="B_ff0000000000000000000000000000000000ff"),
            captured_via="scraper", media=[], now_ms=T2, touch_clocks=False,
        )


def test_writer_crash_before_replace_leaves_original_bytes(data_root, monkeypatch):
    fp = write_entity(
        data_root, GUILD, FEED_ID, feed_body(),
        captured_via="scraper", media=[], now_ms=T1,
    )
    original = fp.read_bytes()

    def exploding_replace(src, dst):
        raise OSError("injected crash between fsync and replace")

    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError, match="injected crash"):
        write_entity(
            data_root, GUILD, FEED_ID, feed_body(title="永不落盘的标题"),
            captured_via="scraper", media=[media_entry(URL_A)], now_ms=T2,
        )

    assert fp.read_bytes() == original  # 原文件字节不变
    tmp = fp.with_suffix(".tmp")
    assert tmp.exists()  # 残留 .tmp 可接受（命名规则：同目录 with_suffix）
    assert json.loads(tmp.read_text(encoding="utf-8"))["_p"]["last_seen"] == T2


def test_writer_fails_loud_on_contract_violations(data_root):
    smuggled = feed_body()
    smuggled["_p"] = {"captured_via": "x", "first_seen": 1, "last_seen": 1, "media": []}
    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, FEED_ID, smuggled,
            captured_via="scraper", media=[], now_ms=T1,
        )

    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, FEED_ID, feed_body(id="B_something_else"),
            captured_via="scraper", media=[], now_ms=T1,
        )

    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, FEED_ID, feed_body(channelInfo={"sign": {"guild_id": "42"}}),
            captured_via="scraper", media=[], now_ms=T1,
        )

    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, COMMENT_ID, comment_body(),
            captured_via="scraper", media=[], now_ms=T1,
        )  # 评论缺 feed_id

    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, FEED_ID, feed_body(),
            captured_via="scraper", media=[], now_ms=T1, feed_id="B_whatever",
        )  # feed 禁传 feed_id

    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, FEED_ID, feed_body(),
            captured_via="scraper", media=[{"status": "pending"}], now_ms=T1,
        )  # media 条目缺 url

    with pytest.raises(PathFormatError):
        write_entity(
            data_root, GUILD, "x_bad", {"id": "x_bad"},
            captured_via="scraper", media=[], now_ms=T1,
        )  # 未知前缀经 paths fail loud

    fp = feed_path(data_root, GUILD, FEED_ID)
    fp.parent.mkdir(parents=True)
    fp.write_text(json.dumps({k: v for k, v in feed_body().items()}), encoding="utf-8")
    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, FEED_ID, feed_body(),
            captured_via="scraper", media=[], now_ms=T1,
        )  # 存量文件缺 _p → raise（无多路回退）

    fp.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(WriterError):
        write_entity(
            data_root, GUILD, FEED_ID, feed_body(),
            captured_via="scraper", media=[], now_ms=T1,
        )
