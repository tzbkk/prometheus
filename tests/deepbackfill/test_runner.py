"""MD-104：runner 全管线（伪 auth 客户端 + 合成树 + 池化媒体 + noauth 评论补齐）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.deepbackfill.runner import BackfillRunner
from src.entity_store import comment_path, feed_path, scan
from src.web_scraper.comments import CommentsScraper
from src.web_scraper.media import MediaDownloadPool, MediaDownloader
from src.web_scraper.store import EntityStore

from tests.deepbackfill.conftest import GUILD

FEED_1 = "B_" + "run" + "0" * 35 + "1"
FEED_2 = "B_" + "run" + "0" * 35 + "2"
FEED_3 = "B_" + "run" + "0" * 35 + "3"
FEED_4 = "B_" + "run" + "0" * 35 + "4"
COMMENT_1 = "c_" + "run" + "0" * 35 + "1"
REPLY_1 = "r_" + "run" + "0" * 35 + "1"

DEFAULT_CHANNEL = "111222333"
IMG_1 = "https://channel.photo.store.qq.com/run-1.png"
IMG_2 = "https://channel.photo.store.qq.com/run-2.png"
IMG_3 = "https://channel.photo.store.qq.com/run-3.png"
IMG_C1 = "https://channel.photo.store.qq.com/run-c1.png"
PNG_1 = b"\x89PNG\r\n\x1a\n" + b"run-1"
PNG_2 = b"\x89PNG\r\n\x1a\n" + b"run-2"
PNG_3 = b"\x89PNG\r\n\x1a\n" + b"run-3"
PNG_C1 = b"\x89PNG\r\n\x1a\n" + b"run-c1"
CDN = {IMG_1: PNG_1, IMG_2: PNG_2, IMG_3: PNG_3, IMG_C1: PNG_C1}

T0 = "1782919600"


def _feed(fid, cc=0, images=None):
    feed = {
        "id": fid,
        "createTime": T0,
        "title": "合成帖子",
        "commentCount": cc,
        "channelInfo": {"sign": {"guild_id": GUILD, "channel_id": DEFAULT_CHANNEL}},
        "postUser": {"id": "u_1", "nick": "作者"},
    }
    if images is not None:
        feed["images"] = images
    return feed


class FakeAuthClient:
    """auth 翻页替身：3 页（2+1+0）→ isFinish 干净终止；游标回填语义；
    频道面：1 默认频道（流已覆盖→跳过）+ 1 新频道 2 页 timeline。"""

    CHANNEL_NEW = "987654321"

    def __init__(self):
        self.pages = [
            ([_feed(FEED_1, cc=2, images=[{"picUrl": IMG_1, "width": 1, "height": 1}]),
              _feed(FEED_2, images=[{"picUrl": IMG_2}])], "p2", False),
            ([_feed(FEED_3)], "p3", False),
            ([], "", True),
        ]
        self.calls: list[str] = []
        self.channel_calls: list[tuple[str, str]] = []

    def get_feeds(self, feed_attch_info=""):
        self.calls.append(feed_attch_info)
        return self.pages[len(self.calls) - 1]

    def get_guild_channels(self):
        return [
            {"channel_id": DEFAULT_CHANNEL, "name": "帖子广场"},
            {"channel_id": self.CHANNEL_NEW, "name": "新频道"},
        ]

    def get_channel_feeds(self, channel_id, feed_attch_info=""):
        self.channel_calls.append((channel_id, feed_attch_info))
        if channel_id == self.CHANNEL_NEW:
            if feed_attch_info == "":
                return ([_feed(FEED_4, images=[{"picUrl": IMG_3}])], "c2", False)
            return ([], "", True)
        return ([], "", True)


class FakeNoauthClient:
    """noauth 评论面替身：FEED_1 一评论（带 vecReply + 评论图）。"""

    def __init__(self):
        self.calls: list[str] = []

    def get_feed_comments(self, feed_id, list_num=20, attch_info=""):
        self.calls.append(feed_id)
        if feed_id != FEED_1:
            return ([], 0, "")
        comment = {
            "id": COMMENT_1,
            "createTime": T0,
            "postUser": {"id": "u_2", "nick": "评论者"},
            "richContents": {"images": [{"picUrl": IMG_C1}]},
            "vecReply": [{
                "id": REPLY_1, "createTime": T0,
                "postUser": {"id": "u_3", "nick": "回复者"},
            }],
        }
        return ([comment], 2, "")


def test_runner_full_history_pipeline_synthetic(data_root):
    scan_result = scan(data_root, GUILD)
    store = EntityStore(data_root, GUILD, dead_urls=scan_result.dead_urls)
    media_pool = MediaDownloadPool(max_workers=2)
    downloader = MediaDownloader(
        store, fetch=lambda url: CDN[url], pool=media_pool
    )
    noauth = FakeNoauthClient()
    ctx = SimpleNamespace(
        guild=SimpleNamespace(guild_id=GUILD),
        auth_client=FakeAuthClient(),
        store=store,
        comments_scraper=CommentsScraper(noauth, store, media_downloader=downloader),
        media_downloader=downloader,
    )
    runner = BackfillRunner([ctx])
    stats = runner.run()
    done, cancelled, running = media_pool.shutdown(timeout=10)
    assert (cancelled, running) == (0, 0) and done >= 3

    # auth 全史翻页：游标逐页回填、isFinish 干净终止、3 页全观测。
    assert ctx.auth_client.calls == ["", "p2", "p3"]
    # 频道走查：混合流不豁免——所有频道都走 timeline；新频道 2 页
    # 补抓 FEED_4，默认频道 1 页空回 isFinish。
    assert ctx.auth_client.channel_calls == [(DEFAULT_CHANNEL, ""),
                                             (FakeAuthClient.CHANNEL_NEW, ""),
                                             (FakeAuthClient.CHANNEL_NEW, "c2")]
    assert stats["pages"] == 6
    assert stats["scanned_feeds"] == 4

    # 实体树：4 feed + 增长检测触发的评论/回复（FEED_1 api_cc=2 > 本地 0）。
    for fid in (FEED_1, FEED_2, FEED_3, FEED_4):
        doc = json.loads(feed_path(data_root, GUILD, fid).read_text(encoding="utf-8"))
        assert doc["_p"]["captured_via"] == "scraper"  # 五键全同：同一写者门面
    assert noauth.calls == [FEED_1]
    comment_doc = json.loads(
        comment_path(data_root, GUILD, COMMENT_1).read_text(encoding="utf-8")
    )
    assert comment_doc["_p"]["feed_id"] == FEED_1
    assert comment_path(data_root, GUILD, REPLY_1).is_file()

    # 媒体池：4 媒体内容寻址落盘 + 状态机 ok。
    media_files = sorted(
        p for p in (data_root / GUILD / "media").rglob("*") if p.is_file()
    )
    assert len(media_files) == 4
    feed_doc = json.loads(feed_path(data_root, GUILD, FEED_1).read_text(encoding="utf-8"))
    assert [e["status"] for e in feed_doc["_p"]["media"]] == ["ok"]
    feed4_doc = json.loads(feed_path(data_root, GUILD, FEED_4).read_text(encoding="utf-8"))
    assert [e["status"] for e in feed4_doc["_p"]["media"]] == ["ok"]

    # 进度计数：feeds/comments/replies 聚合 + running 归位；media 经
    # live_stats 现算（池化异步——dict 快照允许滞后，live 读数才真）。
    assert (stats["feeds"], stats["comments"], stats["replies"]) == (4, 1, 1)
    assert runner.live_stats()["media"] == 4
    assert stats["running"] is False
    assert stats["guilds"][GUILD]["pages"] == 6


def test_runner_stall_keeps_data_and_busy_guard(data_root):
    scan_result = scan(data_root, GUILD)
    store = EntityStore(data_root, GUILD, dead_urls=scan_result.dead_urls)
    media_pool = MediaDownloadPool(max_workers=1)
    downloader = MediaDownloader(store, pool=media_pool)

    class StalledAuth:
        def get_feeds(self, feed_attch_info=""):
            return ([_feed(FEED_1)], feed_attch_info or "stuck", False)

    ctx = SimpleNamespace(
        guild=SimpleNamespace(guild_id=GUILD),
        auth_client=StalledAuth(),
        store=store,
        comments_scraper=CommentsScraper(FakeNoauthClient(), store, media_downloader=downloader),
        media_downloader=downloader,
    )
    runner = BackfillRunner([ctx])
    stats = runner.run()
    media_pool.shutdown(timeout=5)

    # 游标不变 = 异常终止：已落盘数据保真（1 feed 留树），不 raise。
    assert stats["pages"] == 2
    assert feed_path(data_root, GUILD, FEED_1).is_file()
    assert not feed_path(data_root, GUILD, FEED_2).exists()

    # 运行中再触发 = busy（False）——service 409 的判定源。
    gate = _Gate()
    slow_ctx = SimpleNamespace(
        guild=SimpleNamespace(guild_id=GUILD),
        auth_client=_BlockingAuth(gate),
        store=store,
        comments_scraper=CommentsScraper(FakeNoauthClient(), store, media_downloader=downloader),
        media_downloader=downloader,
    )
    slow_runner = BackfillRunner([slow_ctx])
    assert slow_runner.start_background() is True
    try:
        gate.wait_started(timeout=5)
        assert slow_runner.running is True
        assert slow_runner.start_background() is False
    finally:
        gate.release()
        assert slow_runner.join(timeout=5)
    assert slow_runner.running is False


class _Gate:
    def __init__(self):
        import threading

        self._started = threading.Event()
        self._release = threading.Event()

    def wait_started(self, timeout=None):
        assert self._started.wait(timeout)

    def release(self):
        self._release.set()

    def block(self):
        self._started.set()
        self._release.wait()


class _BlockingAuth:
    def __init__(self, gate):
        self._gate = gate

    def get_feeds(self, feed_attch_info=""):
        self._gate.block()
        return ([], "", True)
