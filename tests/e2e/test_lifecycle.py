"""e2e：合成全链演练（一文件串七环，每环消费前序真产物）。

链（module-scope fixture 依文件序触发，失败即断链）：
  1. scan 空树 → 零计数零死集（fresh install 起点）
  2. mock 腾讯响应（build_opener/urlopen 最外层缝）驱动**真** QQWebClient
     → daemon.run_once() → entity_store 写树（2 feed / 1 评论 / 1 回复 / 3 媒体）
  3. scripts/audit_tree.py 子进程对合成树 → exit 0（真 CLI 面）
  4. scripts/archive CLI 时间窗 (20230601, 20230602] 打包 → exit 0 + 解包对账
      （manifest = 内容；包成员 = 窗内树切片镜像）
  5. viewer 临时端口 → 索引计数 = 树计数 = API 计数 + FeedList/FeedDetail Schema
  6. archive CLI dry-run + 引擎枚举 → 窗口计数 = 环 4 manifest 逐字段对账 +
      list_guilds/list_packages 见真包（打包面自身的 e2e 消费——吃环 2 真树 + 环 4 真包）
  7. launcher 临时端口 → GET /targets 三目标 TargetList + scraper start/stop
      幂等（FakeProc 替身零真子进程）

零真实网络门禁：HTTP 全部落在 monkeypatch 缝内。
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import re
import sqlite3
import subprocess
import sys
import tarfile
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import zhizong
import zstandard

from src.archive.engine import list_guilds, list_packages
from src.entity_store import comment_path, feed_path, lock_path, scan
from src.launcher.api import LauncherApi
from src.launcher.process_manager import ProcessManager
from src.web_scraper.urlnorm import normalize_media_url
from src.viewer.backend.indexer import Indexer
from src.viewer.backend.server import ViewerServer
from src.web_scraper.client import QQWebClient
from src.web_scraper.comments import CommentsScraper
from src.web_scraper.daemon import Daemon
from src.web_scraper.feeds import FeedsScraper
from src.web_scraper.media import MediaDownloadPool, MediaDownloader
from src.web_scraper.store import EntityStore

from scripts.archive import main as archive_main

REPO_ROOT = Path(__file__).resolve().parents[2]

GUILD = "7743321643036658"
GUILD_NUMBER = "Takagi3channel"

FEED_IN = "B_" + "e2e" + "0" * 36 + "1"
FEED_OUT = "B_" + "e2e" + "0" * 36 + "2"
COMMENT_IN = "c_" + "e2e" + "0" * 36 + "1"
REPLY_IN = "r_" + "e2e" + "0" * 36 + "1"

FROM_YMD = "20230601"
TO_YMD = "20230602"
# 窗口边界时刻（UTC 整秒）：(from 日零点, to 日末秒]
T_20230601_MID = 1685577600
T_20230602_LAST = 1685750399

IMG_FEED_URL = "https://channel.photo.store.qq.com/e2e-feed.png?dis_k=abc&dis_t=123"
IMG_FEED_NORM = "https://channel.photo.store.qq.com/e2e-feed.png"
VID_FEED_URL = "https://channelvideo.qq.com/e2e-feed.mp4?dis_k=x&dis_t=y"
VID_FEED_NORM = "https://channelvideo.qq.com/e2e-feed.mp4"
IMG_COMMENT_URL = "https://channel.photo.store.qq.com/e2e-comment.png"

PNG_FEED = b"\x89PNG\r\n\x1a\n" + b"e2e-feed-image-bytes"
PNG_COMMENT = b"\x89PNG\r\n\x1a\n" + b"e2e-comment-image-bytes"
MP4_FEED = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2" + b"e2e-video-bytes"

CDN_BYTES = {
    IMG_FEED_NORM: PNG_FEED,
    VID_FEED_NORM: MP4_FEED,
    IMG_COMMENT_URL: PNG_COMMENT,
}

FEED_IN_BODY = {
    "id": FEED_IN,
    "createTime": str(T_20230601_MID + 43200),  # 2023-06-01 12:00
    "title": {"contents": [{"text_content": {"text": "全链演练——窗内帖"}}]},
    "commentCount": 2,
    "channelInfo": {"sign": {"guild_id": GUILD}},
    "postUser": {"id": "u_1", "nick": "作者"},
    "images": [{"picUrl": IMG_FEED_URL, "width": 640, "height": 480}],
    "videos": [{"playUrl": VID_FEED_URL}],
}
FEED_OUT_BODY = {
    "id": FEED_OUT,
    "createTime": "1685786400",  # 2023-06-03 10:00 —— 窗外
    "commentCount": 0,
    "channelInfo": {"sign": {"guild_id": GUILD}},
    "postUser": {"id": "u_2", "nick": "作者乙"},
}
REPLY_BODY = {
    "id": REPLY_IN,
    "createTime": str(T_20230602_LAST),  # 2023-06-02 23:59:59
    "postUser": {"id": "u_4", "nick": "回复者"},
    "richContents": {"contents": [{"text_content": {"text": "全链回复"}}]},
}
COMMENT_BODY = {
    "id": COMMENT_IN,
    "createTime": str(T_20230601_MID + 115200),  # 2023-06-02 08:00
    "postUser": {"id": "u_3", "nick": "评论者"},
    "richContents": {
        "contents": [{"text_content": {"text": "全链评论"}}],
        "images": [{"picUrl": IMG_COMMENT_URL, "width": 100, "height": 80}],
    },
    "vecReply": [REPLY_BODY],
}


class _FakeHTTPResponse:
    """urllib 响应替身：read() + 上下文管理器（真客户端/open 所需最小面）。"""

    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _tencent_rpc(req) -> dict:
    """pd.qq.com noauth RPC 路由表——真 QQWebClient 的响应源（零网络）。"""
    url = req.full_url
    body = json.loads(req.data.decode("utf-8")) if req.data else {}
    if "GetGuildFeeds" in url:
        if body.get("need_channel_list"):
            return {"code": 0, "data": {"channels": []}}
        if not body.get("feedAttchInfo"):
            return {"code": 0, "data": {
                "vecFeed": [FEED_IN_BODY, FEED_OUT_BODY],
                "feedAttchInfo": "page-2", "isFinish": False,
            }}
        return {"code": 0, "data": {"vecFeed": [], "feedAttchInfo": "", "isFinish": True}}
    if "GetChannelTimelineFeeds" in url:
        return {"code": 0, "data": {"vecFeed": [], "feedAttchInfo": "", "isFinish": True}}
    if "GetFeedComments" in url:
        return {"code": 0, "data": {
            "vecComment": [COMMENT_BODY], "totalNum": 2, "attchInfo": "",
        }}
    raise AssertionError(f"unexpected RPC url: {url}")


class _FakeOpener:
    """QQWebClient.opener 替身（build_opener 缝）：JSON RPC → _tencent_rpc。"""

    def __init__(self):
        self.rpc_calls: list[str] = []

    def open(self, req, timeout=None):
        if req.get_method() == "POST":
            self.rpc_calls.append(req.full_url)
            return _FakeHTTPResponse(
                json.dumps(_tencent_rpc(req)).encode("utf-8")
            )
        return _FakeHTTPResponse(b"<html>session-seed</html>")  # _init_session GET


@pytest.fixture(scope="module")
def drill_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("e2e-lifecycle") / "data"
    root.mkdir()
    return root


@pytest.fixture(scope="module")
def captured(drill_root):
    """第 2 环：mock 腾讯响应 → 真客户端/真 daemon → 合成树（后续各环的事实源）。

    媒体经后台池异步落盘（生产形态镜像），run_once 后 drain 再取树。
    """
    with pytest.MonkeyPatch.context() as mp:
        opener = _FakeOpener()
        mp.setattr(urllib.request, "build_opener", lambda *a, **k: opener)
        mp.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=None: _FakeHTTPResponse(
                CDN_BYTES[normalize_media_url(req.full_url)]
            ),
        )
        client = QQWebClient(guild_id=GUILD, guild_number=GUILD_NUMBER)

        result = scan(drill_root, GUILD)
        store = EntityStore(drill_root, GUILD, dead_urls=result.dead_urls)
        store.seed_comment_counts(result.comment_counts, result.reply_counts)
        media_pool = MediaDownloadPool(max_workers=4)
        downloader = MediaDownloader(store, pool=media_pool)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(guild_id=GUILD),
            client=client,
            store=store,
            feeds_scraper=FeedsScraper(client, store, GUILD),
            comments_scraper=CommentsScraper(
                client, store, media_downloader=downloader
            ),
            media_downloader=downloader,
            bottom_reached=False,
            _recheck_cursor=0,
            _recheck_batch_size=50,
            _recheck_workers=3,
        )
        stats: dict = {}
        daemon = Daemon(
            [ctx],
            interval_sec=120,
            stats=stats,
            lock_path=lock_path(drill_root),
        )
        daemon.run_once()
        media_pool.shutdown(timeout=10)

    guild_root = drill_root / GUILD
    feed_files = sorted((guild_root / "feeds").rglob("*.json"))
    comment_files = sorted((guild_root / "comments").rglob("*.json"))
    media_files = sorted(
        p for p in (guild_root / "media").rglob("*") if p.is_file()
    )
    return SimpleNamespace(
        root=drill_root,
        guild_root=guild_root,
        opener=opener,
        feed_files=feed_files,
        comment_files=comment_files,
        media_files=media_files,
        window_entity_paths=[
            feed_path(drill_root, GUILD, FEED_IN),
            comment_path(drill_root, GUILD, COMMENT_IN),
            comment_path(drill_root, GUILD, REPLY_IN),
        ],
        window_counts={"feeds": 1, "comments": 1, "replies": 1},
    )


@pytest.fixture(scope="module")
def packed(captured, tmp_path_factory):
    """第 4 环：CLI --apply 打包 + 解包对账素材（第 6 环 mock 的真数据源）。"""
    archives = tmp_path_factory.mktemp("e2e-archives")
    rc = archive_main([
        "--guild", GUILD,
        "--from", FROM_YMD,
        "--to", TO_YMD,
        "--data-root", str(captured.root),
        "--output", str(archives),
        "--apply",
    ])
    assert rc == 0
    packages = sorted((archives / GUILD / "packages").glob("*.tar.zst"))
    assert len(packages) == 1
    return SimpleNamespace(
        pkg=packages[0],
        archives=archives,
    )


def _open_package(path: Path) -> dict[str, bytes]:
    """解包 tar.zst → {arcname: bytes}（tests/archive/test_engine.py 同形）。"""
    dctx = zstandard.ZstdDecompressor()
    members: dict[str, bytes] = {}
    with open(path, "rb") as fh:
        with dctx.stream_reader(fh) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                for member in tar:
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    members[member.name] = extracted.read()
    return members


@contextmanager
def _live_viewer(db_path, data_dir):
    """临时端口起 ViewerServer（tests/viewer/conftest.py live_viewer 同形）。"""
    server = ViewerServer(port=0, db_path=str(db_path), data_dir=data_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------- 环 1


def test_scan_empty_tree_yields_zero_counts_and_no_dead_urls(data_root):
    result = scan(data_root, GUILD)
    assert result.feeds == 0
    assert result.comment_counts == {}
    assert result.reply_counts == {}
    assert result.dead_urls == set()
    assert result.skipped == 0


# ---------------------------------------------------------------- 环 2


def test_mock_capture_cycle_writes_full_entity_tree(captured):
    assert captured.opener.rpc_calls, "real client must have hit the RPC seam"

    assert len(captured.feed_files) == 2
    assert len(captured.comment_files) == 2  # 1 c_ + 1 r_
    assert len(captured.media_files) == 3  # feed 图 + feed 视频 + 评论图
    assert (captured.root / "prometheus.lock").is_file()

    feed_doc = json.loads(
        feed_path(captured.root, GUILD, FEED_IN).read_text(encoding="utf-8")
    )
    media_urls = [entry["url"] for entry in feed_doc["_p"]["media"]]
    # urlnorm 归一经真 store 生效：dis_k/dis_t 剥除
    assert media_urls == [IMG_FEED_NORM, VID_FEED_NORM]
    assert all(
        entry["status"] == "ok" and entry["file"] for entry in feed_doc["_p"]["media"]
    )

    reply_doc = json.loads(
        comment_path(captured.root, GUILD, REPLY_IN).read_text(encoding="utf-8")
    )
    assert reply_doc["_p"]["feed_id"] == FEED_IN
    # vecReply verbatim 双存：内嵌克隆与 r_ 独立实体并存
    comment_doc = json.loads(
        comment_path(captured.root, GUILD, COMMENT_IN).read_text(encoding="utf-8")
    )
    assert comment_doc["vecReply"][0]["id"] == REPLY_IN


# ---------------------------------------------------------------- 环 3


def test_audit_tree_subprocess_green_on_captured_tree(captured):
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "audit_tree.py"),
            "--data-root", str(captured.root),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT: CLEAN — exit 0" in proc.stdout
    # 实体文件 = 2 feed + c_/r_ 各一；媒体 3；锁 = 范围外 skipped 1
    assert "summary: entities 4 · media 3 · skipped 1 · violations 0" in proc.stdout


# ---------------------------------------------------------------- 环 4


def test_archive_cli_packs_window_and_manifest_reconciles(
    captured, packed, schema_assert
):
    pkg = packed.pkg
    stem = pkg.name[: -len(".tar.zst")]
    assert re.match(
        r"^[0-9]{8}T[0-9]{6}Z_from_20230601_to_20230602$", stem
    ), pkg.name

    members = _open_package(pkg)

    expected_entities = {
        str(path.relative_to(captured.guild_root)): path.read_bytes()
        for path in captured.window_entity_paths
    }
    expected_media = {
        f"media/{p.name[:2]}/{p.name}": p.read_bytes()
        for p in captured.media_files
    }
    assert set(members) == (
        set(expected_entities) | set(expected_media) | {"manifest.json"}
    ), sorted(members)
    for arcname, blob in expected_entities.items():
        assert members[arcname] == blob, arcname  # 镜像 = 字节等价
    for arcname, blob in expected_media.items():
        assert members[arcname] == blob, arcname

    manifest = json.loads(members["manifest.json"])
    schema_assert(manifest, "Manifest")
    assert manifest["window"] == {"from": FROM_YMD, "to": TO_YMD}
    assert manifest["counts"] == captured.window_counts
    assert manifest["guild_id"] == GUILD
    assert manifest["zhizong_version"] == zhizong.__version__
    assert [entry["path"] for entry in manifest["media"]] == sorted(expected_media)
    for entry in manifest["media"]:
        digest = hashlib.sha256(members[entry["path"]]).hexdigest()
        assert entry["sha256"] == digest
        assert entry["path"].rsplit("/", 1)[-1].startswith(digest)  # 名实一致


# ---------------------------------------------------------------- 环 5


def test_viewer_index_api_align_with_captured_tree(
    captured, tmp_path, http, schema_assert
):
    tree = scan(captured.root, GUILD)
    tree_feeds = tree.feeds
    tree_comments = sum(tree.comment_counts.values()) + sum(
        tree.reply_counts.values()
    )  # ViewerStats "含回复" 口径
    tree_media = len(captured.media_files)
    assert (tree_feeds, tree_comments, tree_media) == (2, 2, 3)

    db_path = tmp_path / "viewer.db"
    Indexer(str(db_path)).rebuild_all(captured.root)
    conn = sqlite3.connect(db_path)
    try:
        index_feeds = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        index_comments = conn.execute(
            "SELECT COUNT(*) FROM comments"
        ).fetchone()[0]
        index_media = conn.execute(
            "SELECT COUNT(*) FROM (SELECT file FROM media "
            "UNION SELECT file FROM comment_media)"
        ).fetchone()[0]
    finally:
        conn.close()
    assert (index_feeds, index_comments, index_media) == (
        tree_feeds,
        tree_comments,
        tree_media,
    )

    with _live_viewer(db_path, captured.root) as server:
        base = f"http://127.0.0.1:{server.port}"

        status, body = http("GET", f"{base}/api/stats")
        assert status == 200
        assert body == {
            "feeds": tree_feeds,
            "comments": tree_comments,
            "media": tree_media,
        }

        status, body = http("GET", f"{base}/api/feeds?size=100")
        assert status == 200
        schema_assert(body, "FeedList")
        assert {item["id"] for item in body["feeds"]} == {FEED_IN, FEED_OUT}

        status, detail = http("GET", f"{base}/api/feed/{FEED_IN}")
        assert status == 200
        schema_assert(detail, "FeedDetail")
        media_paths = [entry["path"] for entry in detail["media"]]
        assert len(media_paths) == 2
        assert all(
            re.match(rf"^/media/{GUILD}/[0-9a-f]{{2}}/[0-9a-f]{{64}}\.(png|mp4)$", p)
            for p in media_paths
        ), media_paths  # ㉒-c 后端全路径

        status, body = http("GET", f"{base}/api/feed/{FEED_IN}/comments")
        assert status == 200
        assert [item["id"] for item in body["comments"]] == [COMMENT_IN]


# ---------------------------------------------------------------- 环 6


def test_archive_cli_dry_run_and_enumeration_match_packed_manifest(
    captured, packed, capsys
):
    manifest = json.loads(
        _open_package(packed.pkg)["manifest.json"]
    )
    counts = manifest["counts"]
    media_n = len(manifest["media"])

    rc = archive_main([
        "--guild", GUILD,
        "--from", FROM_YMD,
        "--to", TO_YMD,
        "--data-root", str(captured.root),
        "--output", str(packed.archives),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"feeds={counts['feeds']}" in out
    assert f"comments={counts['comments']}" in out
    assert f"replies={counts['replies']}" in out
    assert f"media={media_n}" in out
    assert "DRY RUN" in out  # 零落包预览（CLI 通道语义）

    # 引擎枚举面吃真树/真包：guild 清单 = 环 2 树，包清单 = 环 4 产物
    assert list_guilds(captured.root) == [GUILD]
    assert list_packages(packed.archives, GUILD) == [
        packed.pkg.name[: -len(".tar.zst")]
    ]


# ---------------------------------------------------------------- 环 7


class _FakeProc:
    """Popen 替身（tests/launcher/conftest.py 同形）：零真子进程。"""

    def __init__(self, pid: int):
        self.pid = pid
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.terminated = True
        return 0


def test_launcher_supervises_three_targets_idempotent(
    tmp_path, monkeypatch, http, schema_assert
):
    import src.launcher.process_manager as pm_module

    manager = ProcessManager(config={})
    manager.project_root = str(tmp_path)
    pids = itertools.count(200_000)
    monkeypatch.setattr(
        pm_module.subprocess, "Popen", lambda *a, **k: _FakeProc(next(pids))
    )
    monkeypatch.setattr(
        pm_module.os,
        "getpgid",
        lambda pid: (_ for _ in ()).throw(ProcessLookupError(pid)),
    )

    api = LauncherApi(
        process_manager=manager,
        config={"launcher_port": 9421, "max_restarts": 5},
        config_path=str(tmp_path / "launcher.conf.json"),
        guilds=[{"guild_id": GUILD}],
        port=0,
    )
    api.start()
    try:
        base = f"http://127.0.0.1:{api.port}"

        status, body = http("GET", f"{base}/targets")
        assert status == 200
        schema_assert(body, "TargetList")
        assert [t["name"] for t in body["targets"]] == [
            "scraper", "deepbackfill", "viewer",
        ]

        status, first = http("POST", f"{base}/targets/scraper/start")
        assert status == 200
        schema_assert(first, "TargetStatus")
        assert first["state"] == "running"

        status, again = http("POST", f"{base}/targets/scraper/start")
        assert status == 200 and again == first  # 幂等：已运行 → 当前态

        status, stopped = http("POST", f"{base}/targets/scraper/stop")
        assert status == 200
        schema_assert(stopped, "TargetStatus")
        assert stopped["state"] == "stopped"

        status, stopped_again = http("POST", f"{base}/targets/scraper/stop")
        assert status == 200 and stopped_again == stopped
    finally:
        api.stop()
