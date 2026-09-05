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
        "months": {
            "202301": {"feeds": 1, "comments": 1, "replies": 0, "days": {1}},
            "202401": {"feeds": 1, "comments": 1, "replies": 0, "days": {1}},
            "202501": {"feeds": 1, "comments": 0, "replies": 1, "days": {1}},
        },
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


def test_stats_lists_month_day_availability(tmp_path):
    """MD-160：逐月计数 + 确切可用日（区间编码，空洞月缺席）。

    span 只是模糊范围；月桶 days 才是可当窗参的 YYYYMMDD 全集——
    连续日压缩为 01-02 式区间，空洞月直接不出现。
    """
    import os as _os

    root = tmp_path / "data"
    shard = root / GUILD / "feeds" / "cc"
    shard.mkdir(parents=True)
    # 2023-01-01 / 01-02 / 01-05（区间+孤岛）/ 03-10（空洞月后）
    for name, ts in (("B_a", 1672531200), ("B_b", 1672617600),
                     ("B_c", 1672876800), ("B_d", 1678406400)):
        with open(_os.path.join(str(shard), name + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"id": name, "createTime": str(ts)}, fh)

    d = Dispatcher(pm=None, config={}, config_path=None,
                   data_root=str(root))
    res = d.dispatch(CommandParser().parse("stats"))

    assert res["ok"] is True
    assert "  202301  3 feeds · 0 comments · 0 replies · days 01-02 05" \
        in res["message"]
    assert "  202303  1 feeds · 0 comments · 0 replies · days 10" \
        in res["message"]
    assert "202302" not in res["message"]
