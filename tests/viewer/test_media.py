"""/media/* 行为层 mandate（MD-057/058，pillar 对应面/C——附带）。

二进制端点不入契约断言（语料无法 JSON 采样）；行为测试覆盖 Range 语义
（206/416 + Content-Range 精确字节）与 Content-Type 按扩展名、路径语法负例、
两段形态兼容路由（索引反查，零前端改动）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from src.entity_store import write_entity
from src.viewer.backend.indexer import Indexer

from tests.viewer.conftest import (
    GUILD_A,
    _feed_body,
    live_viewer,
    media_entry,
    write_media_file,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"media-range-test-png-payload-bytes"
MP4_BYTES = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2" + b"payload"


def _raw_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_media_serving_honors_range_and_content_type(data_root):
    png_name = write_media_file(data_root, GUILD_A, PNG_BYTES)
    mp4_name = write_media_file(data_root, GUILD_A, MP4_BYTES, ext="mp4")
    db_path = Path(data_root).parent / "media-viewer.db"
    with live_viewer(db_path, data_root) as server:
        base = f"http://127.0.0.1:{server.port}"
        png_url = f"{base}/media/{GUILD_A}/{png_name[:2]}/{png_name}"
        total = len(PNG_BYTES)

        # 全量 200 + Content-Type 按扩展名 + Accept-Ranges
        status, headers, body = _raw_get(png_url)
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert headers["Accept-Ranges"] == "bytes"
        assert body == PNG_BYTES

        # 前缀 Range → 206 精确字节 + Content-Range
        status, headers, body = _raw_get(png_url, {"Range": "bytes=0-3"})
        assert status == 206
        assert body == PNG_BYTES[:4]
        assert headers["Content-Range"] == f"bytes 0-3/{total}"
        assert headers["Content-Length"] == "4"

        # 后缀 Range → 206 尾部字节
        status, headers, body = _raw_get(png_url, {"Range": "bytes=-4"})
        assert status == 206
        assert body == PNG_BYTES[-4:]
        assert headers["Content-Range"] == f"bytes {total - 4}-{total - 1}/{total}"

        # 不可满足 → 416 + Content-Range */total
        status, headers, body = _raw_get(png_url, {"Range": f"bytes={total}-999999"})
        assert status == 416
        assert headers["Content-Range"] == f"bytes */{total}"
        assert body == b""

        # 多段 Range → 416（仅单段受支持）
        status, _, _ = _raw_get(png_url, {"Range": "bytes=0-1,3-4"})
        assert status == 416

        # mp4 → video/mp4
        mp4_url = f"{base}/media/{GUILD_A}/{mp4_name[:2]}/{mp4_name}"
        status, headers, body = _raw_get(mp4_url, {"Range": "bytes=4-7"})
        assert status == 206
        assert headers["Content-Type"] == "video/mp4"
        assert body == MP4_BYTES[4:8]

        # 路径负例：错桶（shard 与名不符）→ 403
        wrong_shard = "zz" if png_name[:2] != "zz" else "00"
        status, _, _ = _raw_get(f"{base}/media/{GUILD_A}/{wrong_shard}/{png_name}")
        assert status == 403

        # 路径负例：穿越段（%2e%2e 解码后逃出 media 树）→ 403
        status, _, _ = _raw_get(f"{base}/media/{GUILD_A}/%2e%2e/{png_name}")
        assert status == 403

        # 路径负例：盘上不存在 → 404
        ghost = "0" * 64 + ".png"
        status, _, _ = _raw_get(f"{base}/media/{GUILD_A}/{ghost[:2]}/{ghost}")
        assert status == 404

        # 路径负例：两段形态但未索引（本树无实体引用 → 反查未命中）→ 404
        status, _, _ = _raw_get(f"{base}/media/{GUILD_A}/{png_name}")
        assert status == 404


def test_two_segment_media_url_serves_via_index_reverse_lookup(data_root):
    """MD-058：两段 /media/<guild>/<file> 经 SQLite 媒体索引反查分片路径——
    命中同款文件服务（200 字节相等 + Range 206）；未索引/畸形 → 404 ErrorEnvelope；
    三段 canonical 并行可用（㉒附带：二进制面行为层自由，零前端改动兼容）。"""
    FEED_M = "B_aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    png_name = write_media_file(data_root, GUILD_A, PNG_BYTES)
    write_entity(
        data_root,
        GUILD_A,
        FEED_M,
        _feed_body(FEED_M, GUILD_A, "1782919600"),
        captured_via="scraper",
        media=[media_entry("https://channel.photo.store.qq.com/m.png",
                           file=png_name, status="ok")],
        now_ms=1782919600000,
    )
    db_path = Path(data_root).parent / "media-compat-viewer.db"
    Indexer(str(db_path)).rebuild_all(data_root)

    # 未索引孤儿：盘上存在、无实体引用 → 反查未命中
    orphan_name = write_media_file(data_root, GUILD_A, b"orphan-not-referenced")

    with live_viewer(db_path, data_root) as server:
        base = f"http://127.0.0.1:{server.port}"
        total = len(PNG_BYTES)
        two = f"{base}/media/{GUILD_A}/{png_name}"

        # 两段命中：200 + Content-Type + 字节相等
        status, headers, body = _raw_get(two)
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert body == PNG_BYTES

        # 两段命中：Range 206 精确字节 + Content-Range
        status, headers, body = _raw_get(two, {"Range": "bytes=0-3"})
        assert status == 206
        assert body == PNG_BYTES[:4]
        assert headers["Content-Range"] == f"bytes 0-3/{total}"

        # 三段 canonical 并行可用，同一文件
        status, _, body = _raw_get(
            f"{base}/media/{GUILD_A}/{png_name[:2]}/{png_name}"
        )
        assert status == 200
        assert body == PNG_BYTES

        # 未索引（盘上孤儿）→ 404 ErrorEnvelope
        status, headers, body = _raw_get(f"{base}/media/{GUILD_A}/{orphan_name}")
        assert status == 404
        assert json.loads(body)["error"]["code"] == "not_found"

        # 畸形名（grammar 拒收）→ 404 ErrorEnvelope
        status, _, body = _raw_get(f"{base}/media/{GUILD_A}/not-a-media-name.jpg")
        assert status == 404
        assert json.loads(body)["error"]["code"] == "not_found"

        # 非数字 guild → 404 ErrorEnvelope
        status, _, body = _raw_get(f"{base}/media/notaguild/{png_name}")
        assert status == 404
        assert json.loads(body)["error"]["code"] == "not_found"
