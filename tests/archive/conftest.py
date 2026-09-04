"""tests/archive 套件共享 fixture（engine + CLI 两面）。

- schema_assert：harness 薄胶水同形复制（tests/harness/conftest.py
  同形——服务 fixture 归各测试模块自持）。
- 合成实体树素材：writer 落盘（边界往返），createTime 受控（窗口判据面），
  媒体真实内容寻址（manifest sha256 名实一致面）。
- 基准窗 (20230601, 20230602]：FEED_A 窗内（06-01 12:00）/FEED_B 窗外
  （06-03）/COMMENT_1+REPLY_1 窗内；IMG_SHARED 双实体引用（并集去重面）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from src.entity_store import media_path, write_entity

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"

GUILD = "1000000000000001"

FEED_A = "B_9d8c7b6a5f4e3d2c1b0a9988776655443322110f"
FEED_B = "B_5f2e8a3b9c1d4e6f7a8b9c0d1e2f3a4b5c6d7e"
COMMENT_1 = "c_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"
REPLY_1 = "r_0f9e8d7c6b5a4938271605f4e3d2c1b0a99887"

# 基准窗边界时刻（UTC 整秒）
T_20230601_MID = 1685577600  # 2023-06-01 00:00:00
T_20230602_MID = 1685664000  # 2023-06-02 00:00:00
T_20230603_MID = 1685750400  # 2023-06-03 00:00:00
T_20230602_LAST = T_20230603_MID - 1  # 2023-06-02 23:59:59

T_WRITE_MS = 1782919600000  # _p 观测时钟（与窗口判据无关）

PNG_A = b"\x89PNG\r\n\x1a\n" + b"archive-feed-a-image-bytes"
PNG_C1 = b"\x89PNG\r\n\x1a\n" + b"archive-comment-1-image-bytes"
PNG_SHARED = b"\x89PNG\r\n\x1a\n" + b"archive-shared-image-bytes"
PNG_A_NAME = hashlib.sha256(PNG_A).hexdigest() + ".png"
PNG_C1_NAME = hashlib.sha256(PNG_C1).hexdigest() + ".png"
PNG_SHARED_NAME = hashlib.sha256(PNG_SHARED).hexdigest() + ".png"

URL_A = "https://channel.photo.store.qq.com/feed-a.png"
URL_SHARED = "https://channel.photo.store.qq.com/shared.png"
URL_PENDING = "https://channel.photo.store.qq.com/pending.png"
URL_C1 = "https://channel.photo.store.qq.com/comment-1.png"


@pytest.fixture(scope="session")
def schema_assert():
    """assert_matches(payload, structure_name)——harness 薄胶水同形复制。"""
    from zhizong.compile import compile_structure
    from zhizong.loader import load_corpus

    corpus = load_corpus(CONTRACTS_DIR)
    cache: dict[str, Draft202012Validator] = {}

    def _assert_matches(payload, structure_name):
        if structure_name not in cache:
            schema = compile_structure(corpus.structures()[structure_name], corpus)
            cache[structure_name] = Draft202012Validator(schema)
        errors = sorted(cache[structure_name].iter_errors(payload), key=lambda e: e.path)
        assert not errors, "{0} violations: {1}".format(
            structure_name, [e.message for e in errors]
        )

    return _assert_matches


def media_entry(url: str, **overrides) -> dict:
    """8 字段块全钉形态（，tests/entity_store/test_writer 同款）。"""
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


def write_media_file(data_root, guild: str, content: bytes, ext: str = "png") -> str:
    """内容寻址媒体落盘（⑬-a：名 = sha256 摘要.ext，桶 = 摘要前 2 位）。"""
    name = hashlib.sha256(content).hexdigest() + "." + ext
    path = media_path(data_root, guild, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return name


def write_feed(data_root, guild: str, feed_id: str, create_time: str, media=None) -> None:
    write_entity(
        data_root, guild, feed_id,
        {
            "id": feed_id,
            "createTime": create_time,
            "channelInfo": {"sign": {"guild_id": guild}},
            "title": {"contents": [{"text_content": {"text": "合成帖子"}}]},
        },
        captured_via="scraper",
        media=media or [],
        now_ms=T_WRITE_MS,
    )


def write_comment(
    data_root, guild: str, comment_id: str, create_time: str, feed_id: str, media=None
) -> None:
    write_entity(
        data_root, guild, comment_id,
        {
            "id": comment_id,
            "createTime": create_time,
            "postUser": {"id": "u_2", "nick": "评论者"},
            "richContents": {"contents": [{"text_content": {"text": "合成评论"}}]},
        },
        captured_via="scraper",
        media=media or [],
        now_ms=T_WRITE_MS,
        feed_id=feed_id,
    )


def build_window_tree(data_root) -> SimpleNamespace:
    """基准合成树：窗 (20230601, 20230602] 内 1 feed + 1 comment + 1 reply。

    FEED_B 窗外（06-03）；IMG_SHARED 被 FEED_A 与 COMMENT_1 双引用（并集
    去重面）；IMG_PENDING file=null（pending 不入包）。
    """
    write_media_file(data_root, GUILD, PNG_A)
    write_media_file(data_root, GUILD, PNG_C1)
    write_media_file(data_root, GUILD, PNG_SHARED)

    write_feed(
        data_root, GUILD, FEED_A, str(T_20230601_MID + 43200),  # 06-01 12:00
        media=[
            media_entry(URL_A, file=PNG_A_NAME, status="ok"),
            media_entry(URL_SHARED, file=PNG_SHARED_NAME, status="ok"),
            media_entry(URL_PENDING),
        ],
    )
    write_feed(data_root, GUILD, FEED_B, str(T_20230603_MID + 36000))  # 06-03 10:00
    write_comment(
        data_root, GUILD, COMMENT_1, str(T_20230602_MID + 28800), FEED_A,  # 06-02 08:00
        media=[
            media_entry(URL_SHARED, file=PNG_SHARED_NAME, status="ok"),
            media_entry(URL_C1, file=PNG_C1_NAME, status="ok"),
        ],
    )
    write_comment(
        data_root, GUILD, REPLY_1, str(T_20230602_LAST), FEED_A  # 06-02 23:59:59
    )
    return SimpleNamespace(
        window_counts={"feeds": 1, "comments": 1, "replies": 1},
        window_media=[PNG_A_NAME, PNG_C1_NAME, PNG_SHARED_NAME],
    )
