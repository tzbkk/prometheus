"""媒体下载后台池化 mandate（pillar 对应面）。

MD-093 投递非阻塞：fetch 缝 Event 阻塞下 attempt_entity_media 即时返回，
放行后媒体落盘 + 状态 ok。
MD-094 池并发落盘正确：多实体并发下载（并发峰值>1）全部内容寻址落盘。
MD-095 停机 drain：shutdown 等待/超时取消、池线程零孤儿、幂等、闭池投递 False。
MD-096 投递去重：in-flight 同实体二次投递 False；ok/dead 终态零重复 fetch。
MD-097 启动存量入池：scan().pending_media 恰含 pending|failed 实体，
enqueue_all 种子经池后台落盘（ok/dead 实体不入池）。
MD-098 列表零阻塞：媒体 fetch 全程阻塞下 run_once 整周期（列表→增长评论→
recheck）完成——评论紧随列表完成，不等内联媒体下载。
"""

from __future__ import annotations

import json
import threading
import time

from src.entity_store import scan
from src.entity_store.paths import comment_path, feed_path, media_path
from src.entity_store.writer import write_entity
from src.web_scraper.media import MediaDownloadPool, MediaDownloader, content_name
from src.web_scraper.store import EntityStore, media_block

from tests.web_scraper.conftest import (
    COMMENT_1,
    FEED_A,
    FEED_B,
    FEED_C,
    GUILD,
    IMG_C1,
    IMG_DEAD,
    IMG_OK,
    PNG_OK,
    FakeClient,
    FakeClock,
    build_guild_context,
    synthetic_comment,
    synthetic_feed,
)

_POOL_PREFIX = "media-pool"  # ThreadPoolExecutor 命名 = 前缀_N（下划线）
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _read_feed(data_root, feed_id: str) -> dict:
    return json.loads(feed_path(data_root, GUILD, feed_id).read_text(encoding="utf-8"))


def _media_file(store: EntityStore, entry: dict):
    return media_path(store.data_root, GUILD, entry["file"])


def _media_files_on_disk(data_root) -> list:
    media_dir = data_root / GUILD / "media"
    if not media_dir.is_dir():
        return []
    return sorted(p for p in media_dir.rglob("*") if p.is_file())


def _no_pool_threads_alive(deadline_sec: float = 5.0) -> bool:
    """轮询直至零存活 media-pool-* 线程（或超时 False）——孤儿线程判定面。"""
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        if not any(t.name.startswith(_POOL_PREFIX) for t in threading.enumerate()):
            return True
        time.sleep(0.02)
    return not any(t.name.startswith(_POOL_PREFIX) for t in threading.enumerate())


def _no_tmp_residue(data_root) -> bool:
    return not any(p.suffix == ".tmp" for p in data_root.rglob("*") if p.is_file())


def test_pool_enqueue_returns_immediately_and_download_lands_async(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = EntityStore(data_root, GUILD, clock=FakeClock())
    gate = threading.Event()
    entered = threading.Event()

    def blocked_fetch(url: str) -> bytes:
        entered.set()
        assert gate.wait(timeout=10)
        return PNG_OK

    pool = MediaDownloadPool(max_workers=2)
    downloader = MediaDownloader(store, fetch=blocked_fetch, pool=pool)
    store.save_feed(synthetic_feed(FEED_A, images=[{"picUrl": IMG_OK}]))

    started = time.monotonic()
    returned = downloader.attempt_entity_media(FEED_A)
    elapsed = time.monotonic() - started
    assert returned == 0
    assert elapsed < 0.5, f"enqueue must not block on fetch ({elapsed:.3f}s)"
    assert entered.wait(timeout=5), "pool worker must have started the fetch"
    assert store.media_blocks(FEED_A)[0]["status"] == "pending"  # 投递不动状态机
    assert _media_files_on_disk(data_root) == [], "no file while fetch blocked"

    gate.set()
    done, pending = pool.wait_all(timeout=5)
    assert pending == 0 and done == 1

    doc = _read_feed(data_root, FEED_A)
    entry = doc["_p"]["media"][0]
    assert entry["status"] == "ok"
    assert entry["file"] == content_name(PNG_OK)
    assert _media_file(store, entry).read_bytes() == PNG_OK
    assert downloader.downloaded_count == 1
    pool.shutdown(timeout=5)
    assert _no_pool_threads_alive()


def test_pool_downloads_many_entities_concurrently_to_disk(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = EntityStore(data_root, GUILD, clock=FakeClock())
    n_feeds = 6
    feeds = [f"B_{'pool':<37}{i:03d}" for i in range(n_feeds)]
    blobs: dict[str, bytes] = {}
    for fid in feeds:
        url = f"https://cdn.example.com/{fid}.png"
        blobs[url] = _PNG_HEADER + f"bytes-of-{fid}".encode()
        store.save_feed(synthetic_feed(fid, images=[{"picUrl": url}]))

    gauge = threading.Lock()
    active = 0
    peak = 0

    def tracking_fetch(url: str) -> bytes:
        nonlocal active, peak
        with gauge:
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with gauge:
            active -= 1
        return blobs[url]

    pool = MediaDownloadPool(max_workers=3)
    downloader = MediaDownloader(store, fetch=tracking_fetch, pool=pool)
    for fid in feeds:
        assert downloader.attempt_entity_media(fid) == 0

    done, pending = pool.wait_all(timeout=10)
    assert pending == 0 and done == n_feeds
    assert peak > 1, "downloads must actually run concurrently in pool threads"

    for fid in feeds:
        entry = _read_feed(data_root, fid)["_p"]["media"][0]
        assert entry["status"] == "ok", fid
        assert _media_file(store, entry).read_bytes() == blobs[entry["url"]]
    assert downloader.downloaded_count == n_feeds
    pool.shutdown(timeout=5)
    assert _no_pool_threads_alive()


def test_pool_shutdown_drains_inflight_and_is_idempotent(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = EntityStore(data_root, GUILD, clock=FakeClock())
    gate = threading.Event()
    entered = threading.Semaphore(0)

    def blocked_fetch(url: str) -> bytes:
        entered.release()
        assert gate.wait(timeout=10)
        return PNG_OK

    pool = MediaDownloadPool(max_workers=2)
    downloader = MediaDownloader(store, fetch=blocked_fetch, pool=pool)
    for fid in (FEED_A, FEED_B):
        store.save_feed(synthetic_feed(fid, images=[{"picUrl": IMG_OK}]))
        assert pool.enqueue(downloader, fid) is True
    assert entered.acquire(timeout=5) and entered.acquire(timeout=5)

    done, cancelled, running = pool.shutdown(timeout=0.2)
    assert (done, cancelled, running) == (0, 0, 2), "both jobs running past timeout"
    assert pool.closed
    assert pool.enqueue(downloader, FEED_A) is False, "closed pool must refuse quietly"

    gate.set()
    assert _no_pool_threads_alive(), "no media-pool-* threads may survive shutdown"
    for fid in (FEED_A, FEED_B):  # 在途任务跑完落盘——drain 干净、无半写
        entry = _read_feed(data_root, fid)["_p"]["media"][0]
        assert entry["status"] == "ok" and _media_file(store, entry).is_file()
    assert _no_tmp_residue(data_root)

    again = pool.shutdown(timeout=5)  # 幂等：二唤不 raise，回放首报
    assert again == (done, cancelled, running)


def test_pool_enqueue_dedups_inflight_and_terminal_media_not_refetched(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    write_entity(
        data_root,
        GUILD,
        FEED_C,
        synthetic_feed(FEED_C),
        captured_via="scraper",
        media=[media_block(IMG_DEAD, media_type="image", status="dead")],
        now_ms=1,
    )
    startup = scan(data_root, GUILD)
    store = EntityStore(data_root, GUILD, dead_urls=startup.dead_urls, clock=FakeClock())

    gate = threading.Event()
    fetches: list[str] = []
    lock = threading.Lock()

    def counting_fetch(url: str) -> bytes:
        with lock:
            fetches.append(url)
        assert gate.wait(timeout=10)
        return PNG_OK

    pool = MediaDownloadPool(max_workers=1)
    downloader = MediaDownloader(store, fetch=counting_fetch, pool=pool)

    store.save_feed(synthetic_feed(FEED_A, images=[{"picUrl": IMG_OK}]))
    store.save_feed(synthetic_feed(FEED_B, images=[{"picUrl": IMG_DEAD}]))
    assert store.media_blocks(FEED_B)[0]["status"] == "dead"  # 死集先查：立块即终态

    assert pool.enqueue(downloader, FEED_A) is True
    assert pool.enqueue(downloader, FEED_A) is False, "in-flight entity must dedup"
    gate.set()
    done, pending = pool.wait_all(timeout=5)
    assert pending == 0 and done == 1
    assert fetches == [IMG_OK], "dedup collapses duplicates; dead = zero fetch"

    assert pool.enqueue(downloader, FEED_A) is True  # retired 后可再投
    assert pool.enqueue(downloader, FEED_B) is True  # dead 实体可投、零网络
    done, pending = pool.wait_all(timeout=5)
    assert pending == 0 and done == 3, "cumulative pool completions: 2×FEED_A + FEED_B"
    assert fetches == [IMG_OK], "ok/dead 终态不重复投递——零重复 fetch"

    entry_a = _read_feed(data_root, FEED_A)["_p"]["media"][0]
    assert entry_a["status"] == "ok" and entry_a["retries"] == 1
    entry_b = _read_feed(data_root, FEED_B)["_p"]["media"][0]
    assert entry_b["status"] == "dead" and entry_b["retries"] == 0
    pool.shutdown(timeout=5)
    assert _no_pool_threads_alive()


def test_startup_scan_reports_pending_media_and_pool_seeds_backlog(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    write_entity(
        data_root,
        GUILD,
        FEED_A,
        synthetic_feed(FEED_A),
        captured_via="scraper",
        media=[media_block(IMG_OK, media_type="image", status="ok")],
        now_ms=1,
    )
    write_entity(
        data_root,
        GUILD,
        FEED_B,
        synthetic_feed(FEED_B),
        captured_via="scraper",
        media=[media_block(IMG_OK, media_type="image")],
        now_ms=1,
    )
    failed_block = media_block(IMG_C1, media_type="image")
    failed_block["status"] = "failed"
    failed_block["retries"] = 1
    write_entity(
        data_root,
        GUILD,
        COMMENT_1,
        synthetic_comment(COMMENT_1),
        captured_via="scraper",
        media=[failed_block],
        now_ms=1,
        feed_id=FEED_C,
    )
    write_entity(
        data_root,
        GUILD,
        FEED_C,
        synthetic_feed(FEED_C),
        captured_via="scraper",
        media=[media_block(IMG_DEAD, media_type="image", status="dead")],
        now_ms=1,
    )

    result = scan(data_root, GUILD)
    assert result.pending_media == {FEED_B: 1, COMMENT_1: 1}, (
        "only pending|failed entities qualify; ok-only/dead-only stay out"
    )

    fetches: list[str] = []
    store = EntityStore(data_root, GUILD, dead_urls=result.dead_urls, clock=FakeClock())

    def recording_fetch(url: str) -> bytes:
        fetches.append(url)
        return PNG_OK

    pool = MediaDownloadPool(max_workers=2)
    downloader = MediaDownloader(store, fetch=recording_fetch, pool=pool)

    accepted = pool.enqueue_all(downloader, result.pending_media)
    assert accepted == 2
    done, pending = pool.wait_all(timeout=5)
    assert pending == 0 and done == 2

    feed_b = _read_feed(data_root, FEED_B)["_p"]["media"][0]
    assert feed_b["status"] == "ok" and _media_file(store, feed_b).is_file()
    comment_doc = json.loads(
        comment_path(data_root, GUILD, COMMENT_1).read_text(encoding="utf-8")
    )
    assert comment_doc["_p"]["media"][0]["status"] == "ok"
    assert sorted(fetches) == [IMG_C1, IMG_OK], "dead url stays zero-fetch"
    pool.shutdown(timeout=5)
    assert _no_pool_threads_alive()


def test_daemon_listing_unblocked_and_comments_follow_immediately(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()

    gate = threading.Event()
    order: list[tuple[str, str]] = []

    class OrderRecordingClient(FakeClient):
        def get_feed_comments(self, feed_id, list_num=20, attch_info=""):
            order.append(("comments", feed_id))
            return super().get_feed_comments(feed_id, list_num, attch_info)

    def blocked_fetch(url: str) -> bytes:
        order.append(("media-start", url))
        assert gate.wait(timeout=10)
        return _PNG_HEADER + url.encode()

    feeds = []
    comments = {}
    for i in range(5):
        fid = f"B_{'dmn':<38}{i:02d}"
        feeds.append(
            synthetic_feed(
                fid,
                comment_count=1,
                images=[{"picUrl": f"https://cdn.example.com/{i}.png"}],
            )
        )
        comments[fid] = [synthetic_comment(f"c_{'dmn':<37}{i:02d}")]

    pool = MediaDownloadPool(max_workers=2)
    harness = build_guild_context(
        data_root,
        OrderRecordingClient(feeds, comments),
        fetch=blocked_fetch,
        media_pool=pool,
    )

    harness.daemon.run_once()  # 整周期同步返回——媒体 fetch 全程未放行

    assert not gate.is_set(), "cycle must never have waited on media downloads"
    assert any(kind == "comments" for kind, _ in order), (
        "growth comment scrape must run inside the cycle"
    )
    assert len(order) >= 10, "media-start(≥2) + growth comments(5) + recheck(5) recorded"
    saved = sorted(p.name for p in (data_root / GUILD / "feeds").rglob("*.json"))
    assert len(saved) == 5, "full listing observed while media fetch blocked"

    gate.set()
    done, pending = pool.wait_all(timeout=10)
    assert pending == 0 and done >= 5
    for feed in feeds:
        entry = _read_feed(data_root, feed["id"])["_p"]["media"][0]
        assert entry["status"] == "ok", feed["id"]
        assert _media_file(harness.store, entry).read_bytes() == (
            _PNG_HEADER + entry["url"].encode()
        )
    pool.shutdown(timeout=5)
    assert _no_pool_threads_alive()


def test_sniff_accepts_gif_channelr_animated_stickers():
    """MD-125 channelr psc 动图贴纸 = GIF87a/89a（image/gif，最大 ~3MB）——
    sniff 认 gif，content_name 出 .gif。"""
    from src.web_scraper.media import content_name, sniff_ext

    assert sniff_ext(b"GIF89a" + b"\x00" * 64) == "gif"
    assert sniff_ext(b"GIF87a" + b"\x00" * 64) == "gif"
    name = content_name(b"GIF89a" + b"\x00" * 64)
    assert name is not None and name.endswith(".gif")
    from src.entity_store.paths import _MEDIA_FILE_RE

    assert _MEDIA_FILE_RE.match(name), name
