"""实体路径/分片/锁 mandate 测试（MD-012 ~ MD-015，pillar 对应面）。

契约溯源：structures/{Feed,Comment,Reply,MediaAsset,ProcessLock}.yaml Location 模板。
全部合成数据 + tmp_path 数据根；锁的存活探测走假 os.kill（确定性，不依赖真实 pid 空间）。
"""

from __future__ import annotations

import json
import os

import pytest

from src.entity_store import (
    LockFormatError,
    PathFormatError,
    ProcessLockData,
    comment_path,
    feed_path,
    is_stale,
    lock_path,
    media_path,
    read_lock,
    resolve,
    shard_of,
    write_lock,
)

# 默认 /proc/sys/kernel/pid_max 上界为 2**22，此值不可能对应任何真实存活进程
DEAD_PID = 2**22

GUILD = "7743321643036658"
FEED_ID = "B_0123456789abcdef0123456789abcdef"
COMMENT_ID = "c_0123456789abcdef"
REPLY_ID = "r_fedcba9876543210"
MEDIA_FILE = "ab" + "0" * 62 + ".jpg"


def test_entity_paths_round_trip_construct_and_resolve(data_root):
    p = feed_path(data_root, GUILD, FEED_ID)
    assert p == data_root / GUILD / "feeds" / f"B_{shard_of(FEED_ID)}" / f"{FEED_ID}.json"
    ident = resolve(p)
    assert (ident.kind, ident.guild, ident.id) == ("feed", GUILD, FEED_ID)
    assert ident.shard == shard_of(FEED_ID)

    cp = comment_path(data_root, GUILD, COMMENT_ID)
    rp = comment_path(data_root, GUILD, REPLY_ID)
    assert cp.parent == data_root / GUILD / "comments" / f"c_{shard_of(COMMENT_ID)}"
    assert rp.parent == data_root / GUILD / "comments" / f"r_{shard_of(REPLY_ID)}"
    ident = resolve(cp)
    assert (ident.kind, ident.id) == ("comment", COMMENT_ID)
    ident = resolve(rp)
    assert (ident.kind, ident.id) == ("reply", REPLY_ID)

    mp = media_path(data_root, GUILD, MEDIA_FILE)
    assert mp == data_root / GUILD / "media" / MEDIA_FILE[:2] / MEDIA_FILE
    ident = resolve(mp)
    assert (ident.kind, ident.guild, ident.id) == ("media", GUILD, MEDIA_FILE)


def test_entity_paths_fail_loud_on_malformed_id_and_path(data_root):
    with pytest.raises(PathFormatError):
        feed_path(data_root, GUILD, "x_bad")
    with pytest.raises(PathFormatError):
        feed_path(data_root, GUILD, "B_")
    with pytest.raises(PathFormatError):
        comment_path(data_root, GUILD, "B_also_a_feed")
    with pytest.raises(PathFormatError):
        comment_path(data_root, GUILD, "x_bad")
    with pytest.raises(PathFormatError):
        media_path(data_root, GUILD, "deadbeef.png")
    with pytest.raises(PathFormatError):
        media_path(data_root, GUILD, "abc.jpg")
    with pytest.raises(PathFormatError):
        feed_path(data_root, GUILD, f"c_{COMMENT_ID}/../../{FEED_ID}")

    with pytest.raises(PathFormatError):
        resolve("x_bad")
    with pytest.raises(PathFormatError):
        resolve(data_root / GUILD / "feeds" / "B_xx" / f"{FEED_ID}.json")
    with pytest.raises(PathFormatError):
        resolve(data_root / GUILD / "nope" / "c_00" / f"{COMMENT_ID}.json")

    good = f"c_{COMMENT_ID[2:]}"
    wrong_bucket = "00" if shard_of(good) != "00" else "01"
    with pytest.raises(PathFormatError):
        resolve(data_root / GUILD / "comments" / f"c_{wrong_bucket}" / f"{good}.json")


def test_entity_shard_distribution_covers_two_hex_buckets():
    buckets = set()
    for n in range(1024):
        key = f"B_{n:064x}"
        s = shard_of(key)
        assert len(s) == 2
        assert all(ch in "0123456789abcdef" for ch in s)
        assert shard_of(key) == s
        buckets.add(s)
    assert len(buckets) >= 128


def test_process_lock_round_trip_and_stale_pid_verdict(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    lp = lock_path(root)
    assert lp == root / "prometheus.lock"
    assert read_lock(lp) is None

    lock = ProcessLockData(pid=1234, dirty=True, cycle=7, ts=1725000000, bottomReached=False)
    write_lock(lp, lock)
    assert not lp.with_suffix(".tmp").exists()
    assert read_lock(lp) == lock

    raw = json.loads(lp.read_text(encoding="utf-8"))
    assert list(raw) == ["pid", "dirty", "cycle", "ts", "bottomReached"]
    assert type(raw["ts"]) is int

    missing = dict(raw)
    missing.pop("bottomReached")
    lp.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(LockFormatError):
        read_lock(lp)

    extra = dict(raw)
    extra["oops"] = 1
    lp.write_text(json.dumps(extra), encoding="utf-8")
    with pytest.raises(LockFormatError):
        read_lock(lp)

    write_lock(lp, lock)

    def fake_kill(pid, sig, *args, **kwargs):
        if sig == 0:
            if pid == DEAD_PID:
                raise ProcessLookupError(3, "No such process")
            return None
        raise AssertionError(f"only sig=0 probes expected, got sig={sig}")

    monkeypatch.setattr(os, "kill", fake_kill)
    dead = ProcessLockData(pid=DEAD_PID, dirty=True, cycle=1, ts=1, bottomReached=False)
    live = ProcessLockData(pid=os.getpid(), dirty=False, cycle=2, ts=2, bottomReached=True)
    assert is_stale(dead) is True
    assert is_stale(live) is False
    assert lp.exists()


def test_write_lock_creates_missing_data_root_parents(tmp_path):
    root = tmp_path / "fresh-clone" / "data"  # 全链父目录不存在
    lp = lock_path(root)

    lock = ProcessLockData(pid=4242, dirty=False, cycle=1, ts=1730000000,
                           bottomReached=False)
    write_lock(lp, lock)  # 不得 FileNotFoundError（fresh-clone 首启锁写崩溃循环根因）

    assert read_lock(lp) == lock  # 往返等价
    assert not lp.with_suffix(".tmp").exists()  # 原子写无残留
    assert lp.parent == root  # 目录即建在锁所在根
