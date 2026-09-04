"""tests/viewer 套件共享 fixture。

- http / schema_assert：harness 薄胶水同形复制（tests/harness/conftest.py
  同形——服务 fixture 归各测试模块自持）。
- 合成实体树素材：writer 落盘（边界往返），双 guild / 3 feed / 2 comment+reply /
  媒体三态（ok+盘上文件、pending、dead）；FEED_BARE 无 title/poster（FeedList
  nullable 字段实例源）。
- live_viewer：临时端口起 ViewerServer（port=0 + daemon 线程 + shutdown 收尾）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from src.entity_store import media_path, write_entity
from src.viewer.backend.indexer import Indexer
from src.viewer.backend.server import ViewerServer

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"

GUILD_A = "1000000000000001"
GUILD_B = "1000000000000002"

FEED_A = "B_9d8c7b6a5f4e3d2c1b0a9988776655443322110f"
FEED_BARE = "B_5f2e8a3b9c1d4e6f7a8b9c0d1e2f3a4b5c6d7e"
FEED_C = "B_11223344556677889900aabbccddeeff00112233"
FEED_NEW = "B_fedcba9876543210fedcba9876543210fedcba98"
COMMENT_1 = "c_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a"
REPLY_1 = "r_0f9e8d7c6b5a4938271605f4e3d2c1b0a99887"

T1 = 1782919600000
T2 = 1782930000000

PNG_A = b"\x89PNG\r\n\x1a\n" + b"viewer-feed-a-image-bytes"
PNG_C1 = b"\x89PNG\r\n\x1a\n" + b"viewer-comment-1-image-bytes"
PNG_A_NAME = hashlib.sha256(PNG_A).hexdigest() + ".png"
PNG_C1_NAME = hashlib.sha256(PNG_C1).hexdigest() + ".png"

URL_A = "https://channel.photo.store.qq.com/feed-a.png"
URL_PENDING = "https://channel.photo.store.qq.com/pending.png"
URL_DEAD = "https://channel.photo.store.qq.com/dead.png"
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


@pytest.fixture
def http():
    """http(method, url, payload=None) -> (status, body)——urllib 薄客户端（同形）。"""

    def _request(method, url, payload=None, timeout=5):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read())

    def _parse(raw):
        text = raw.decode("utf-8")
        try:
            return json.loads(text)
        except ValueError:
            return text

    return _request


def media_entry(url: str, **overrides) -> dict:
    """8 字段块全钉形态（，与 tests/entity_store/test_writer 同款）。"""
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
    """内容寻址媒体文件落盘（：名 = sha256 摘要.ext，桶 = 摘要前 2 位）。"""
    name = hashlib.sha256(content).hexdigest() + "." + ext
    path = media_path(data_root, guild, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return name


def _feed_body(feed_id: str, guild: str, create_time: str, **extra) -> dict:
    body = {
        "id": feed_id,
        "createTime": create_time,
        "channelInfo": {"sign": {"guild_id": guild}},
        "commentCount": 2,
    }
    body.update(extra)
    return body


def build_tree(data_root) -> SimpleNamespace:
    """合成双 guild 树（writer 落盘，边界往返）。

    GUILD_A：FEED_A（标题两段/poster/2 图 1 视频/like 5/媒体三态：ok+盘上 png、
              pending、dead）、FEED_BARE（最小载荷——无 title/poster，nullable 实例源）、
              COMMENT_1（两段文本、likeInfo.count 字符串、sequence 1、ok 媒体+盘上文件）、
              REPLY_1（targetReplyID、无 sequence）
    GUILD_B：FEED_C（标题一段、poster、无媒体）
    """
    write_media_file(data_root, GUILD_A, PNG_A)
    write_media_file(data_root, GUILD_A, PNG_C1)

    write_entity(
        data_root, GUILD_A, FEED_A,
        _feed_body(
            FEED_A, GUILD_A, "1782919600",
            title={"contents": [
                {"text_content": {"text": "合成帖子标题"}},
                {"text_content": {"text": "第二段"}},
            ]},
            contents={"contents": [
                {"text_content": {"text": "合成帖子标题 第二段 这是被预览截断的完整正文尾部"}},
            ]},
            poster={"id": "u_1", "nick": "作者甲",
                    "icon": {"iconUrl": "https://thirdqq.qq.com/a.jpg"}},
            images=[{"picUrl": "https://x/1.png"}, {"picUrl": "https://x/2.png"}],
            videos=[{"videoUrl": "https://x/v.mp4"}],
            total_like={"like_count": 5},
        ),
        captured_via="scraper",
        media=[
            media_entry(URL_A, file=PNG_A_NAME, status="ok", width=100, height=80),
            media_entry(URL_PENDING),
            media_entry(URL_DEAD, status="dead"),
        ],
        now_ms=T1,
    )
    write_entity(
        data_root, GUILD_A, FEED_BARE,
        _feed_body(FEED_BARE, GUILD_A, "1782919700"),
        captured_via="scraper", media=[], now_ms=T1,
    )
    write_entity(
        data_root, GUILD_B, FEED_C,
        _feed_body(
            FEED_C, GUILD_B, "1782919500",
            title={"contents": [{"text_content": {"text": "频道B帖子"}}]},
            poster={"id": "u_9", "nick": "作者丙"},
        ),
        captured_via="scraper", media=[], now_ms=T1,
    )
    write_entity(
        data_root, GUILD_A, COMMENT_1,
        {
            "id": COMMENT_1,
            "createTime": "1782920526",
            "postUser": {"id": "u_2", "nick": "评论者乙",
                         "icon": {"iconUrl": "https://thirdqq.qq.com/b.jpg"}},
            "richContents": {
                "contents": [
                    {"text_content": {"text": "评论一段"}},
                    {"text_content": {"text": "评论二段"}},
                ],
                "ip_location_province": "浙江",
            },
            "likeInfo": {"count": "3"},
            "replyCount": 1,
            "sequence": 1,
        },
        captured_via="scraper",
        media=[media_entry(URL_C1, file=PNG_C1_NAME, status="ok")],
        now_ms=T1,
        feed_id=FEED_A,
    )
    write_entity(
        data_root, GUILD_A, REPLY_1,
        {
            "id": REPLY_1,
            "createTime": "1782920600",
            "postUser": {"id": "u_3", "nick": "回复者丁"},
            "richContents": {"contents": [{"text_content": {"text": "楼中楼文本"}}]},
            "targetReplyID": COMMENT_1,
        },
        captured_via="scraper", media=[], now_ms=T1, feed_id=FEED_A,
    )
    return SimpleNamespace(
        feeds=3, comments=2, media=2,
        guild_a_feeds=2, guild_b_feeds=1, feed_a_comments=2,
    )


@contextmanager
def live_viewer(db_path, data_dir):
    """临时端口起 ViewerServer（port=0；serve_forever daemon 线程 + shutdown 收尾）。"""
    server = ViewerServer(port=0, db_path=str(db_path), data_dir=data_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def viewer_site(data_root):
    """合成树 + 全量索引 + 活体服务（三级对齐/六端点/rebuild 测试的公共底座）。"""
    counts = build_tree(data_root)
    db_path = Path(data_root).parent / "viewer.db"
    Indexer(str(db_path)).rebuild_all(data_root)
    with live_viewer(db_path, data_root) as server:
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{server.port}",
            server=server,
            data_root=data_root,
            db_path=db_path,
            counts=counts,
        )
