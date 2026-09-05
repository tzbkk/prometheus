"""launcher `stats` 动词——每 guild 实体树计数（MD-147/148）。

背景：前身 ``tail stats`` 与已删除的 ``tail feeds`` 曾读平铺旧格式
``data/feeds.jsonl``（实体树世界不存在，完全不可用）。现读
``data/{guild}/`` 实体树：feeds、c_ 评论、r_ 回复、媒体文件与字节，
附 total 行。
"""

from __future__ import annotations

import json
import os

from src.launcher.commands import (
    CommandParser,
    Dispatcher,
    archive_tree_stats,
)

GUILD = "1000000000000001"


TS_2023 = 1672531200  # 2023-01-01 00:00:00 UTC
TS_2024 = 1704067200  # 2024-01-01 00:00:00 UTC
TS_2025 = 1735689600  # 2025-01-01 00:00:00 UTC


def _write_feed(root, guild, feed_id, mtime, create_time):
    shard_dir = os.path.join(root, guild, "feeds", feed_id[-2:])
    os.makedirs(shard_dir, exist_ok=True)
    path = os.path.join(shard_dir, feed_id + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"id": feed_id, "createTime": str(create_time)}, fh)
    os.utime(path, (mtime, mtime))


def _write_entity(root, guild, shard_prefix, name, mtime, create_time):
    shard_dir = os.path.join(root, guild, "comments", shard_prefix)
    os.makedirs(shard_dir, exist_ok=True)
    path = os.path.join(shard_dir, name + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"id": name, "createTime": str(create_time)}, fh)
    os.utime(path, (mtime, mtime))


def _write_media(root, guild, name, size, mtime):
    shard_dir = os.path.join(root, guild, "media", name[:2])
    os.makedirs(shard_dir, exist_ok=True)
    path = os.path.join(shard_dir, name)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    os.utime(path, (mtime, mtime))


def _build_tree(root):
    _write_feed(root, GUILD, "B_feedccc", 3000, TS_2023)
    _write_feed(root, GUILD, "B_feedbbb", 2000, TS_2024)
    _write_feed(root, GUILD, "B_feedaaa", 1000, TS_2025)
    _write_entity(root, GUILD, "c_ab", "c_c1", 1000, TS_2023)
    _write_entity(root, GUILD, "c_cd", "c_c2", 1000, TS_2024)
    _write_entity(root, GUILD, "r_ab", "r_r1", 1000, TS_2025)
    _write_media(root, GUILD, "aa.png", 2048, 1000)
    _write_media(root, GUILD, "bb.jpg", 512, 1000)


def test_stats_counts_entity_tree_and_media_bytes(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    _build_tree(str(root))

    stats = archive_tree_stats(str(root))

    assert stats[GUILD] == {
        "feeds": 3, "comments": 2, "replies": 1,
        "media_files": 2, "media_bytes": 2560,
        "earliest_ts": TS_2023, "latest_ts": TS_2025,
    }

    d = Dispatcher(pm=None, config={}, config_path=None,
                   data_root=str(root))
    res = d.dispatch(CommandParser().parse("stats"))
    line = res["message"].splitlines()[0]
    assert "{0}  3 feeds · 2 comments · 1 replies · 2 media (2.5 KB)".format(
        GUILD) in line
    assert "total  3 feeds · 2 comments · 1 replies · 2 media (2.5 KB)" in (
        res["message"])



def test_stats_verb_empty_tree_has_readable_message(tmp_path):
    empty = tmp_path / "data"
    empty.mkdir()
    d = Dispatcher(pm=None, config={}, config_path=None,
                   data_root=str(empty))

    assert d.dispatch(
        CommandParser().parse("stats"))["message"] == (
            "No archive found under {0}.".format(empty))


def test_stats_reports_data_spans_and_archive_usage(tmp_path):
    """MD-157：stats 行附 createTime 跨度 + 尾随 archive 用法行。

    跨度 YYYYMMDD 与窗参同格式——看一眼即可拼 archive 命令；
    usage 行给窗语义（(from, to]，UTC）。
    """
    root = tmp_path / "data"
    root.mkdir()
    _build_tree(str(root))

    d = Dispatcher(pm=None, config={}, config_path=None,
                   data_root=str(root))
    res = d.dispatch(CommandParser().parse("stats"))

    assert res["ok"] is True
    assert "· span 20230101..20250101" in res["message"]
    assert "usage: archive <guild> <from> <to> [--apply]" in res["message"]
    assert "(UTC YYYYMMDD)" in res["message"]
