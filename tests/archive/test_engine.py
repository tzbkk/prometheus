"""打包引擎 mandate——边界 4（树→包→解包对账）+ 窗口数学 + 打包前对账。

MD-059（pillar 对应面）：包 = 实体树切片镜像 + 媒体并集 + manifest.json；解包
对账 manifest = 实际包内容逐字段（窗口/计数/媒体清单/哈希/生成时刻/版本戳）。
MD-060（pillar 对应面）：(from, to] 半开窗参数表——边界秒归属 + from==to 单日窗 +
三类实体各自按自身 createTime 判窗。
MD-061（pillar 对应面）：打包前对账 fail loud——计数不一致树（宽容遍历会静默丢
实体的各种形态）/媒体缺盘/名实不符 → ReconciliationError；.tmp 写者崩溃
残留不阻塞（纪律内 artifact）。
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest
import zhizong
import zstandard

from src.archive.engine import (
    ReconciliationError,
    WindowError,
    list_guilds,
    list_packages,
    plan_package,
    window_bounds,
    write_package,
)
from src.entity_store import comment_path

from tests.archive.conftest import (
    COMMENT_1,
    FEED_B,
    GUILD,
    PNG_SHARED_NAME,
    T_20230601_MID,
    T_20230602_LAST,
    T_20230602_MID,
    T_20230603_MID,
    build_window_tree,
    media_entry,
    write_comment,
    write_feed,
)

CREATED_MS = 1690000000000  # 2023-07-22T04:26:40Z（钉死包名/manifest 时刻）

FEED_AT_FROM_MID = "B_1111111111111111111111111111111111111111"
FEED_AFTER_FROM = "B_2222222222222222222222222222222222222222"
FEED_AT_TO_LAST = "B_3333333333333333333333333333333333333333"
FEED_AFTER_TO = "B_4444444444444444444444444444444444444444"
COMMENT_OWNCLOCK = "c_5555555555555555555555555555555555555555"


def _open_package(path: Path) -> dict[str, bytes]:
    """解包 tar.zst → {arcname: bytes}（流式 zstd 解压）。"""
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


def test_window_package_mirrors_tree_slice_and_manifest_matches_content(
    data_root, tmp_path, schema_assert
):
    counts = build_window_tree(data_root)
    archives = tmp_path / "archives"

    plan = plan_package(data_root, GUILD, "20230601", "20230602")
    assert plan.counts == counts.window_counts
    assert [ref.name for ref in plan.media] == sorted(counts.window_media)

    out_path = write_package(plan, archives, created_ms=CREATED_MS)
    assert out_path == archives / GUILD / "packages" / (
        "20230722T042640Z_from_20230601_to_20230602.tar.zst"
    )

    members = _open_package(out_path)
    guild_root = Path(data_root) / GUILD
    expected_entity_members = {
        str(path.relative_to(guild_root)): path.read_bytes()
        for path in sorted(guild_root.rglob("*.json"))
        if FEED_B not in path.name  # FEED_B createTime 06-03 —— 窗外切片排除
    }
    expected_media_members = {
        f"media/{name[:2]}/{name}": (guild_root / "media" / name[:2] / name).read_bytes()
        for name in counts.window_media
    }
    for arcname, blob in expected_entity_members.items():
        assert members[arcname] == blob  # 镜像 = 字节等价
    for arcname, blob in expected_media_members.items():
        assert members[arcname] == blob
    assert set(members) == set(expected_entity_members) | set(expected_media_members) | {
        "manifest.json"
    }

    # 解包对账：manifest = 实际包内容逐字段（pillar 对应面 边界 4）
    manifest = json.loads(members["manifest.json"])
    schema_assert(manifest, "Manifest")
    assert manifest["window"] == {"from": "20230601", "to": "20230602"}
    assert manifest["counts"] == counts.window_counts
    assert manifest["created_at"] == "2023-07-22T04:26:40Z"
    assert manifest["zhizong_version"] == zhizong.__version__
    assert manifest["guild_id"] == GUILD
    actual_media_members = sorted(n for n in members if n.startswith("media/"))
    assert [entry["path"] for entry in manifest["media"]] == actual_media_members
    for entry in manifest["media"]:
        digest = hashlib.sha256(members[entry["path"]]).hexdigest()
        assert entry["sha256"] == digest  # 清单哈希 = 包内实字节摘要
        assert entry["path"].rsplit("/", 1)[-1].startswith(digest)
    # IMG_SHARED 双实体引用 → 并集去重为单成员/单条目
    shared_entries = [e for e in manifest["media"] if e["path"].endswith(PNG_SHARED_NAME)]
    assert len(shared_entries) == 1
    assert len([n for n in members if n.endswith(PNG_SHARED_NAME)]) == 1


def test_window_math_parameter_table(data_root, tmp_path):
    write_feed(data_root, GUILD, FEED_AT_FROM_MID, str(T_20230601_MID))
    write_feed(data_root, GUILD, FEED_AFTER_FROM, str(T_20230601_MID + 1))
    write_feed(data_root, GUILD, FEED_AT_TO_LAST, str(T_20230602_LAST))
    write_feed(data_root, GUILD, FEED_AFTER_TO, str(T_20230603_MID))
    # comment 按自身 createTime 判窗（其父帖 FEED_AFTER_TO 在 06-03）
    write_comment(
        data_root, GUILD, COMMENT_OWNCLOCK, str(T_20230602_MID + 50000), FEED_AFTER_TO
    )

    # (from, to] 整日窗：from 日零点排除 / to 日末秒含入 / 次日零点排除
    wide = plan_package(data_root, GUILD, "20230601", "20230602")
    assert wide.counts == {"feeds": 2, "comments": 1, "replies": 0}
    assert {e.path.name.removesuffix(".json") for e in wide.entries} == {
        FEED_AFTER_FROM, FEED_AT_TO_LAST, COMMENT_OWNCLOCK,
    }

    # from == to 合法（单日窗，纯日期窗的自然推论）
    day2 = plan_package(data_root, GUILD, "20230602", "20230602")
    assert {e.path.name.removesuffix(".json") for e in day2.entries} == {
        FEED_AT_TO_LAST, COMMENT_OWNCLOCK,
    }

    day1 = plan_package(data_root, GUILD, "20230601", "20230601")
    assert {e.path.name.removesuffix(".json") for e in day1.entries} == {
        FEED_AFTER_FROM,
    }

    # 空窗：计数全零、is_empty（不落包语义的判据面）
    empty = plan_package(data_root, GUILD, "20200601", "20200602")
    assert empty.is_empty and empty.entries == () and empty.media == ()

    # 界值换算钉面：(YYYYMMDD 对) → (整秒 from, 整秒 to]
    assert window_bounds("20230601", "20230602") == (T_20230601_MID, T_20230602_LAST)
    with pytest.raises(WindowError):
        window_bounds("20230602", "20230601")  # 倒序
    with pytest.raises(WindowError):
        window_bounds("99999999", "20260830")  # 非法日历
    with pytest.raises(WindowError):
        window_bounds("20220101", "20991231")  # 未来窗（越界）
    with pytest.raises(WindowError):
        window_bounds("2022-1-1", "20260830")  # 非 8 位数字


def test_prepack_reconciliation_fails_loud(data_root, tmp_path):
    build_window_tree(data_root)
    guild_root = Path(data_root) / GUILD

    # ① 损坏 JSON：宽容 iter_window 会静默跳过 → 计数不一致 → exit 3 面
    corrupt = comment_path(data_root, GUILD, COMMENT_1).with_suffix(".json")
    original = corrupt.read_bytes()
    corrupt.write_bytes(b"{ torn write")
    with pytest.raises(ReconciliationError, match="reconciliation failed"):
        plan_package(data_root, GUILD, "20230601", "20230602")
    corrupt.write_bytes(original)  # 复原供后续形态复用同一棵树

    # ② id 与文件名不符（宽容遍历跳过 → 不入账 → 对账红）
    doc = json.loads(original)
    doc["id"] = "c_9999999999999999999999999999999999999999"
    corrupt.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ReconciliationError, match="reconciliation failed"):
        plan_package(data_root, GUILD, "20230601", "20230602")
    corrupt.write_bytes(original)

    # ③ 引用媒体缺盘
    missing_feed = "B_6666666666666666666666666666666666666666"
    write_feed(
        data_root, GUILD, missing_feed, str(T_20230601_MID + 100),
        media=[media_entry("https://x/missing.png", file="0" * 64 + ".png", status="ok")],
    )
    with pytest.raises(ReconciliationError, match="missing on disk"):
        plan_package(data_root, GUILD, "20230601", "20230602")
    from src.entity_store import feed_path

    feed_path(data_root, GUILD, missing_feed).unlink()  # 移除 ③ 形态供 ④⑤ 复用

    # ④ 名实不符（内容 sha256 ≠ 文件名摘要段，⑬-a 违例）
    plan_probe = plan_package(data_root, GUILD, "20230601", "20230602")
    victim = plan_probe.media[0]
    media_disk = guild_root / "media" / victim.name[:2] / victim.name
    original_bytes = media_disk.read_bytes()
    media_disk.write_bytes(b"tampered payload")  # 仍 grammar 合法名，内容已变
    with pytest.raises(ReconciliationError, match="name/content mismatch"):
        plan_package(data_root, GUILD, "20230601", "20230602")
    media_disk.write_bytes(original_bytes)  # 复原供 ⑤ 复用

    # ⑤ .tmp 写者崩溃残留 = 纪律内 artifact，不阻塞打包
    feed_shard_dir = (guild_root / "feeds").rglob("B_*")
    stray = next(feed_shard_dir) / "orphan.json.tmp"
    stray.write_text("{}", encoding="utf-8")
    restored = plan_package(data_root, GUILD, "20230101", "20230602")
    assert restored.counts["feeds"] >= 1  # stray 未入账亦未阻塞


def test_package_enumeration_faces(data_root, tmp_path):
    build_window_tree(data_root)
    assert list_guilds(data_root) == [GUILD]
    assert list_guilds(tmp_path / "nowhere") == []

    archives = tmp_path / "archives"
    plan = plan_package(data_root, GUILD, "20230601", "20230602")
    assert list_packages(archives, GUILD) == []
    write_package(plan, archives, created_ms=CREATED_MS)
    write_package(
        plan_package(data_root, GUILD, "20230101", "20230602"),
        archives,
        created_ms=CREATED_MS + 2000_000,
    )
    names = list_packages(archives, GUILD)
    assert len(names) == 2  # 创建时刻降序（名降序）
    assert names[0].startswith("20230722T050000Z")
    assert names == sorted(names, reverse=True)
    assert list_packages(archives, "9999999999999999") == []
    all_guilds = list_packages(archives)
    assert sorted(all_guilds) == sorted(names)
    # 非 grammar 包名（手放文件）不进清单
    junk = archives / GUILD / "packages" / "backup-final.tar.zst"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"junk")
    assert len(list_packages(archives, GUILD)) == 2
