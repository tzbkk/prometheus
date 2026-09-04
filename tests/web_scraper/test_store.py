"""合成抓取 run 的树纪律与 conformance mandate（pillar 对应面）。

MD-044 path containment：run 后 tmp 数据根 rglob 全集 ⊆ {实体 json, 媒体
文件, prometheus.lock}——零 bookkeeping 文件（jsonl/ids/state/checkpoints
任何形态即红）。
MD-045 每写盘 conformance：全部实体过编译 Schema +
_p 键序 + ok 块文件名语法与磁盘实存。
MD-046 畸形腾讯载荷 → skip+log 不落盘（boundary fail loud）。
"""

from __future__ import annotations

import json
import logging
import re

import pytest
from jsonschema import Draft202012Validator

from src.entity_store.paths import media_path, shard_of
from src.web_scraper.media import MediaDownloadPool
from src.web_scraper.store import SKIPPED

from tests.web_scraper.conftest import (
    COMMENT_1,
    COMMENT_2,
    FEED_A,
    FEED_B,
    GUILD,
    IMG_A,
    IMG_A_NORM,
    IMG_C1,
    IMG_R1,
    REPLY_1,
    VID_A,
    FakeClient,
    FakeClock,
    build_guild_context,
    synthetic_comment,
    synthetic_feed,
    synthetic_reply,
)

MEDIA_NAME_RE = re.compile(r"^[0-9a-f]{64}\.(jpg|png|mp4|gif)$")
BOOKKEEPING_NAMES = (
    "feeds.jsonl",
    "comments.jsonl",
    "ids",
    "comment_keys",
    "comments_fetched_ids",
    "state.json",
    "checkpoints.jsonl",
    "media_index.jsonl",
    "comment_media_index.jsonl",
    "dead_media_permanent.jsonl",
)


@pytest.fixture
def synth(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    feeds = [
        synthetic_feed(
            FEED_A,
            comment_count=2,
            images=[{"picUrl": IMG_A, "width": 640, "height": 480}],
            videos=[{"playUrl": VID_A}],
        ),
        synthetic_feed(FEED_B, comment_count=0),
    ]
    comments = {
        FEED_A: [
            synthetic_comment(
                COMMENT_1,
                replies=[synthetic_reply(REPLY_1, images=[{"picUrl": IMG_R1, "width": 64, "height": 64}])],
                images=[{"picUrl": IMG_C1, "width": 100, "height": 80}],
            ),
            synthetic_comment(COMMENT_2),
        ],
    }
    # 生产形态镜像——媒体经后台池异步落盘，断言前 drain
    pool = MediaDownloadPool(max_workers=4)
    harness = build_guild_context(
        data_root, FakeClient(feeds, comments), clock=FakeClock(), media_pool=pool
    )
    harness.daemon.run_once()
    pool.shutdown(timeout=10)
    return harness


def test_synthetic_cycle_writes_only_entity_media_lock_files(synth):
    data_root = synth.store.data_root
    files = sorted(p for p in data_root.rglob("*") if p.is_file())
    assert files, "synthetic run must produce files"

    for path in files:
        rel = path.relative_to(data_root)
        if rel.name == "prometheus.lock":
            continue
        parts = rel.parts
        assert len(parts) == 4, f"non-tree path leaked: {rel}"
        _guild, kind, shard, name = parts
        assert _guild == GUILD
        if kind == "media":
            assert re.match(r"^[0-9a-f]{2}$", shard), f"bad media shard: {rel}"
            assert MEDIA_NAME_RE.match(name), f"bad media file name: {rel}"
        elif kind == "feeds":
            assert re.match(r"^B_[0-9a-f]{2}$", shard), f"bad feed shard: {rel}"
            assert name.endswith(".json")
        elif kind == "comments":
            assert re.match(r"^[cr]_[0-9a-f]{2}$", shard), f"bad comment shard: {rel}"
            assert name.endswith(".json")
        else:
            pytest.fail(f"unexpected directory in data tree: {rel}")

    for bookkeeping in BOOKKEEPING_NAMES:
        for hit in data_root.rglob(bookkeeping):
            pytest.fail(f"bookkeeping file resurrected: {hit}")

    guild_root = data_root / GUILD
    feed_files = list((guild_root / "feeds").rglob("*.json"))
    comment_files = list((guild_root / "comments").rglob("*.json"))
    media_files = [p for p in (guild_root / "media").rglob("*") if p.is_file()]
    assert len(feed_files) == 2
    assert len(comment_files) == 3  # c_ + c_ + r_
    assert len(media_files) == 4  # feed img + feed video + comment img + reply img
    assert (data_root / "prometheus.lock").is_file()


def test_every_written_entity_passes_compiled_schemas(synth, compiled_schemas):
    guild_root = synth.store.data_root / GUILD
    entities = sorted(
        list((guild_root / "feeds").rglob("*.json"))
        + list((guild_root / "comments").rglob("*.json"))
    )
    assert len(entities) == 5

    for path in entities:
        doc = json.loads(path.read_text(encoding="utf-8"))
        schema_name = "Feed" if doc["id"].startswith("B_") else (
            "Comment" if doc["id"].startswith("c_") else "Reply"
        )
        Draft202012Validator(compiled_schemas[schema_name]).validate(doc)
        assert list(doc)[-1] == "_p", f"_p must be the last key: {path}"

        p = doc["_p"]
        if schema_name == "Feed":
            assert list(p) == ["captured_via", "first_seen", "last_seen", "media"]
            assert p["captured_via"] == "scraper"
        else:
            assert list(p) == ["feed_id", "captured_via", "first_seen", "last_seen", "media"]
            assert p["feed_id"] == FEED_A

        for entry in p["media"]:
            assert list(entry) == [
                "url", "download_url", "file", "type", "width", "height",
                "status", "retries", "last_attempt_ts",
            ]
            assert entry["status"] == "ok"
            assert entry["file"] and MEDIA_NAME_RE.match(entry["file"])
            assert entry["retries"] == 1
            assert isinstance(entry["last_attempt_ts"], int)
            on_disk = media_path(synth.store.data_root, GUILD, entry["file"])
            assert on_disk.is_file(), f"media block references missing file: {entry['file']}"

    feed_a_doc = json.loads(
        (guild_root / "feeds" / f"B_{shard_of(FEED_A)}" / f"{FEED_A}.json").read_text(
            encoding="utf-8"
        )
    )
    media_urls = {entry["url"] for entry in feed_a_doc["_p"]["media"]}
    assert media_urls == {IMG_A_NORM, "https://channelvideo.qq.com/feed-a.mp4"}


def test_malformed_tencent_payloads_skip_and_log_without_writes(tmp_path, caplog):
    from src.web_scraper.store import EntityStore

    data_root = tmp_path / "data"
    data_root.mkdir()
    store = EntityStore(data_root, GUILD, clock=FakeClock())

    malformed_feeds = [
        "not-an-object",
        {"createTime": "1782919600", "channelInfo": {"sign": {"guild_id": GUILD}}},
        {"id": "c_wrong_prefix", "createTime": "1", "channelInfo": {"sign": {"guild_id": GUILD}}},
        {"id": FEED_A, "createTime": 1782919600, "channelInfo": {"sign": {"guild_id": GUILD}}},
        {"id": FEED_A, "createTime": "1782919600"},
        {"id": FEED_A, "createTime": "1782919600", "channelInfo": {"sign": {}}},
        {"id": FEED_A, "createTime": "1782919600", "channelInfo": {"sign": {"guild_id": "999"}}},
    ]
    malformed_comments = [
        "not-an-object",
        {"id": FEED_A, "createTime": "1"},
        {"id": COMMENT_1},
        {"id": COMMENT_1, "createTime": "1782920526"},
    ]

    with caplog.at_level(logging.WARNING, logger="src.web_scraper.store"):
        for payload in malformed_feeds:
            assert store.save_feed(payload) == SKIPPED
        for payload in malformed_comments[:3]:
            assert store.save_comment(payload, feed_id=FEED_A) == SKIPPED
        assert (
            store.save_comment(malformed_comments[3], feed_id="not-a-feed") == SKIPPED
        )

    assert len(caplog.records) == len(malformed_feeds) + len(malformed_comments)
    assert [p for p in data_root.rglob("*") if p != data_root] == []


def test_media_download_url_identity_refresh_and_legacy_revive(tmp_path):
    """MD-124：urlnorm 剥掉的 dis_k/dis_t 是下载必需凭证——归一 url 只当
    身份/死集键，下载走 download_url（原签地址，复观刷新）；无
    download_url 键的块 dead/failed 复观复活 pending；有 download_url
    键的块真死不扰。"""
    from src.web_scraper.media import MediaDownloader
    from src.web_scraper.store import EntityStore
    from tests.web_scraper.conftest import MP4_A

    store = EntityStore(tmp_path, GUILD)
    signed = "https://qchannelvideo.qq.com/v1.mp4?dis_k=AAA&dis_t=111&os=6"
    signed2 = "https://qchannelvideo.qq.com/v1.mp4?dis_k=BBB&dis_t=222&os=6"
    bare = "https://qchannelvideo.qq.com/v1.mp4?os=6"  # os 非易变参，归一保留

    def vid_of(blocks):
        return [b for b in blocks if b["type"] == "video"][0]

    store.save_feed(synthetic_feed(FEED_A, videos=[{"playUrl": signed}]))
    block = vid_of(store.media_blocks(FEED_A))
    assert block["url"] == bare and block["download_url"] == signed

    fetched: list[str] = []

    def rec_fetch(url: str) -> bytes:
        fetched.append(url)
        return MP4_A

    MediaDownloader(store, fetch=rec_fetch).attempt_entity_media_now(FEED_A)
    assert fetched == [signed]
    assert vid_of(store.media_blocks(FEED_A))["status"] == "ok"

    store.save_feed(synthetic_feed(FEED_A, videos=[{"playUrl": signed2}]))
    blocks = [b for b in store.media_blocks(FEED_A) if b["type"] == "video"]
    assert len(blocks) == 1
    assert blocks[0]["download_url"] == signed2 and blocks[0]["status"] == "ok"

    legacy = [dict(b) for b in store.media_blocks(FEED_A)]
    for b in legacy:
        b.pop("download_url", None)
        if b["type"] == "video":
            b["status"], b["retries"] = "dead", 3
    store.rewrite_media(FEED_A, legacy)
    store.save_feed(synthetic_feed(FEED_A, videos=[{"playUrl": signed2}]))
    assert vid_of(store.media_blocks(FEED_A))["status"] == "pending"

    newera = [dict(b) for b in store.media_blocks(FEED_A)]
    for b in newera:
        if b["type"] == "video":
            b["status"], b["retries"] = "dead", 3
    store.rewrite_media(FEED_A, newera)
    store.save_feed(synthetic_feed(FEED_A, videos=[{"playUrl": signed2}]))
    assert vid_of(store.media_blocks(FEED_A))["status"] == "dead"
