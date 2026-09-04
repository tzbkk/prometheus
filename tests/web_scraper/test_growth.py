"""增长检测 + 媒体死集 mandate（pillar 对应面，机制两用）。

MD-047 growth_targets 参数表：API commentCount vs 本地评论文件数的无记忆
比较全分支（表驱动循环，非 parametrize——mandate 双射纪律）。
MD-048 死集先查 + failed 晋升经 writer + 媒体尝试时钟冻结：scan 派生死集
→ 新条目直接标 dead 零网络；三次失败 → writer 写前晋升 dead → 集合回灌；
touch_clocks=False 两本时钟不动。
"""

from __future__ import annotations

import json
from urllib.error import URLError

from src.entity_store import scan
from src.entity_store.paths import feed_path
from src.entity_store.writer import write_entity
from src.web_scraper.media import MediaDownloader
from src.web_scraper.store import EntityStore, growth_targets, media_block

from tests.web_scraper.conftest import (
    FEED_B,
    FEED_C,
    FEED_D,
    GUILD,
    IMG_DEAD,
    IMG_FLAKY,
    IMG_OK,
    PNG_OK,
    FakeClock,
    synthetic_feed,
)


def test_growth_targets_parameter_table():
    cases = [
        ({"f1": 5}, {"f1": 3}, {"f1"}),  # API 增长 → 重拉
        ({"f1": 3}, {"f1": 3}, set()),  # 持平 → 不重拉
        ({"f1": 2}, {"f1": 3}, set()),  # API 落后（远端删除）→ 不重拉
        ({"f1": 1}, {}, {"f1"}),  # 本地无账（首拉）→ 重拉
        ({"f1": 0, "f2": 0}, {"f2": 9}, set()),  # API 零计数 → 不重拉
        ({}, {"f1": 9}, set()),  # API 空面 → 不重拉
        ({}, {}, set()),  # 双空
        ({"a": 4, "b": 1, "c": 2}, {"a": 4, "c": 5}, {"b"}),  # 混合
    ]
    for api_counts, local_counts, expected in cases:
        assert growth_targets(api_counts, local_counts) == expected, (
            api_counts,
            local_counts,
        )


def test_dead_urls_prechecked_and_failed_media_promoted_via_writer(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    clock = FakeClock()

    write_entity(
        data_root,
        GUILD,
        FEED_B,
        synthetic_feed(FEED_B),
        captured_via="scraper",
        media=[media_block(IMG_DEAD, media_type="image", status="dead")],
        now_ms=clock(),
    )
    startup = scan(data_root, GUILD)
    assert startup.dead_urls == {IMG_DEAD}

    store = EntityStore(data_root, GUILD, dead_urls=startup.dead_urls, clock=clock)
    fetched: list[str] = []

    def flaky_cdn(url: str) -> bytes:
        fetched.append(url)
        if url == IMG_FLAKY:
            raise URLError("synthetic CDN failure")
        return PNG_OK

    downloader = MediaDownloader(store, fetch=flaky_cdn)

    store.save_feed(
        synthetic_feed(FEED_C, images=[{"picUrl": IMG_DEAD}, {"picUrl": IMG_OK}])
    )
    doc = _read_feed(data_root, FEED_C)
    statuses = {e["url"]: e["status"] for e in doc["_p"]["media"]}
    assert statuses == {IMG_DEAD: "dead", IMG_OK: "pending"}  # 死集先查：新条目直接标 dead

    downloader.attempt_entity_media(FEED_C)
    doc = _read_feed(data_root, FEED_C)
    by_url = {e["url"]: e for e in doc["_p"]["media"]}
    assert IMG_DEAD not in fetched  # 死 URL 零网络调用
    assert by_url[IMG_OK]["status"] == "ok" and by_url[IMG_OK]["file"]
    assert doc["_p"]["first_seen"] == doc["_p"]["last_seen"] == clock.now  # 时钟冻结

    store.save_feed(synthetic_feed(FEED_D, images=[{"picUrl": IMG_FLAKY}]))
    for _ in range(3):
        clock.tick()
        downloader.attempt_entity_media(FEED_D)

    doc = _read_feed(data_root, FEED_D)
    flaky = doc["_p"]["media"][0]
    assert flaky["status"] == "dead"  # retries==3 → writer 写前晋升
    assert flaky["retries"] == 3 and flaky["file"] is None
    assert doc["_p"]["first_seen"] == doc["_p"]["last_seen"]  # 晋升亦不动观测时钟
    assert IMG_FLAKY in store.dead_urls  # 晋升回灌 in-memory 死集

    store.save_feed(
        synthetic_feed(FEED_C, images=[{"picUrl": IMG_FLAKY}, {"picUrl": IMG_OK}])
    )
    doc = _read_feed(data_root, FEED_C)
    statuses = {e["url"]: e["status"] for e in doc["_p"]["media"]}
    assert statuses[IMG_FLAKY] == "dead"  # 回灌后：另一实体的新引用直接标 dead
    assert statuses[IMG_OK] == "ok"  # 同 url 存量块逐字保留——状态机不重置
    assert fetched.count(IMG_FLAKY) == 3  # 死集命中后零重复抓取


def _read_feed(data_root, feed_id: str) -> dict:
    return json.loads(feed_path(data_root, GUILD, feed_id).read_text(encoding="utf-8"))
