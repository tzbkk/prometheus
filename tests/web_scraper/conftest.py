"""tests/web_scraper 套件共享 fixture。

- compiled_schemas：conformance fixture（同形自持，域内零跨目录
  import；zhizong IO 校验器落地后整块迁出，测试面不动）。
- http / schema_assert：harness 薄胶水复制（tests/harness/conftest.py
  同形——服务 fixture 归各服务测试模块自持）。
- 合成抓取素材：FakeClient（鸭子类型 QQWebClient）+ fetch 缝 + FakeClock，
  全程零网络（conftest 信号守卫 + 数据根惯例之上）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
ENTITY_STRUCTURES = ("Feed", "Comment", "Reply")

GUILD = "1000000000000001"
GUILD_NUMBER = "Takagi3channel"

FEED_A = "B_9d8c7b6a5f4e3d2c1b0a9988776655443322110f"
FEED_B = "B_11223344556677889900aabbccddeeff00112233"
FEED_C = "B_fedcba9876543210fedcba9876543210fedcba98"
FEED_D = "B_0123456789abcdef0123456789abcdef01234567"
COMMENT_1 = "c_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
COMMENT_2 = "c_0f9e8d7c6b5a4938271605f4e3d2c1b0a9988776"
REPLY_1 = "r_aaaa1111bbbb2222cccc3333dddd4444eeee5555"

T_START = 1782919600000

IMG_A = "https://channel.photo.store.qq.com/feed-a.png?dis_k=abc&dis_t=123"
IMG_A_NORM = "https://channel.photo.store.qq.com/feed-a.png"
VID_A = "https://channelvideo.qq.com/feed-a.mp4?dis_k=x&dis_t=y"
VID_A_NORM = "https://channelvideo.qq.com/feed-a.mp4"
IMG_C1 = "https://channel.photo.store.qq.com/comment-1.png"
IMG_R1 = "https://channel.photo.store.qq.com/reply-1.png"
IMG_DEAD = "https://channel.photo.store.qq.com/dead.png"
IMG_FLAKY = "https://channel.photo.store.qq.com/flaky.png"
IMG_OK = "https://channel.photo.store.qq.com/ok.png"

PNG_A = b"\x89PNG\r\n\x1a\n" + b"feed-a-image-bytes"
PNG_C1 = b"\x89PNG\r\n\x1a\n" + b"comment-1-image-bytes"
PNG_R1 = b"\x89PNG\r\n\x1a\n" + b"reply-1-image-bytes"
PNG_OK = b"\x89PNG\r\n\x1a\n" + b"ok-image-bytes"
MP4_A = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2" + b"video-bytes"

CDN_BYTES = {
    IMG_A_NORM: PNG_A,
    VID_A_NORM: MP4_A,
    IMG_C1: PNG_C1,
    IMG_R1: PNG_R1,
    IMG_OK: PNG_OK,
}


@pytest.fixture(scope="module")
def compiled_schemas() -> dict[str, dict]:
    """{"Feed": schema, "Comment": schema, "Reply": schema}——zhizong 编译产物。"""
    from zhizong.compile import compile_structure
    from zhizong.loader import load_corpus

    corpus = load_corpus(CONTRACTS_DIR)
    return {
        name: compile_structure(corpus.structures()[name], corpus)
        for name in ENTITY_STRUCTURES
    }


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


class FakeClock:
    """可推进墙钟：() -> int unix ms。"""

    def __init__(self, start: int = T_START):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def tick(self, ms: int = 1000) -> int:
        self.now += ms
        return self.now


class FakeClient:
    """QQWebClient 鸭子替身：单页列表 + 单页评论，零网络。"""

    def __init__(self, feeds: list[dict], comments: dict[str, list[dict]]):
        self.feeds = feeds
        self.comments = comments

    def get_feeds(self, from_: int = 7, feed_attch_info: str = ""):
        if feed_attch_info == "" and self.feeds:
            return (list(self.feeds), "page-2", False)
        return ([], "", True)

    def get_guild_channels(self):
        return []

    def get_channel_feeds(self, channel_id: str, from_: int = 7, feed_attch_info: str = ""):
        return ([], "", True)

    def get_feed_comments(self, feed_id: str, list_num: int = 20, attch_info: str = ""):
        page = self.comments.get(feed_id, [])
        return (list(page), len(page), "")


def synthetic_feed(feed_id: str, *, comment_count: int = 0, images=None, videos=None) -> dict:
    feed = {
        "id": feed_id,
        "createTime": "1782919600",
        "title": "合成帖子——中文直存",
        "commentCount": comment_count,
        "channelInfo": {"sign": {"guild_id": GUILD}},
        "postUser": {"id": "u_1", "nick": "作者"},
    }
    if images is not None:
        feed["images"] = images
    if videos is not None:
        feed["videos"] = videos
    return feed


def synthetic_comment(comment_id: str, *, replies=None, images=None) -> dict:
    node = {
        "id": comment_id,
        "createTime": "1782920526",
        "content": "合成评论",
        "postUser": {"id": "u_2", "nick": "评论者"},
    }
    if images is not None:
        node["richContents"] = {"images": images}
    if replies is not None:
        node["vecReply"] = replies
    return node


def synthetic_reply(reply_id: str, *, images=None) -> dict:
    node = {
        "id": reply_id,
        "createTime": "1782920600",
        "content": "合成回复",
        "postUser": {"id": "u_3", "nick": "回复者"},
    }
    if images is not None:
        node["richContents"] = {"images": images}
    return node


def default_cdn_fetch(url: str) -> bytes:
    # 伪 CDN 签名不敏感：dis_k/dis_t 是凭证不是内容（真 CDN 同义）。
    from src.web_scraper.urlnorm import normalize_media_url

    return CDN_BYTES[normalize_media_url(url)]


def build_guild_context(data_root: Path, client, *, fetch=None, clock=None, media_pool=None):
    """__main__._build_components 的单 guild 镜像：scan 种子 → 全组件组装
    （media_pool 传入即池化投递——生产形态镜像）。"""
    from src.entity_store import scan
    from src.web_scraper.comments import CommentsScraper
    from src.web_scraper.daemon import Daemon
    from src.web_scraper.feeds import FeedsScraper
    from src.web_scraper.media import MediaDownloader
    from src.web_scraper.store import EntityStore

    result = scan(data_root, GUILD)
    store = EntityStore(data_root, GUILD, dead_urls=result.dead_urls, clock=clock)
    store.seed_comment_counts(result.comment_counts, result.reply_counts)
    downloader = MediaDownloader(store, fetch=fetch or default_cdn_fetch, pool=media_pool)
    feeds_scraper = FeedsScraper(client, store, GUILD)
    comments_scraper = CommentsScraper(client, store, media_downloader=downloader)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(guild_id=GUILD),
        client=client,
        store=store,
        feeds_scraper=feeds_scraper,
        comments_scraper=comments_scraper,
        media_downloader=downloader,
        bottom_reached=False,
        _recheck_cursor=0,
        _recheck_batch_size=50,
        _recheck_workers=3,
    )
    stats = {
        "scanned_feeds": 0,
        "feeds": 0,
        "comments": 0,
        "replies": 0,
        "media": 0,
        "last_scan_ts": 0,
        "daemon_running": False,
        "log_buffer": [],
        "guilds": {},
    }
    from src.entity_store import lock_path

    daemon = Daemon(
        [ctx],
        interval_sec=120,
        stats=stats,
        lock_path=lock_path(data_root),
        now_ms=clock,
    )
    return SimpleNamespace(
        ctx=ctx,
        store=store,
        downloader=downloader,
        daemon=daemon,
        stats=stats,
        media_pool=media_pool,
    )
