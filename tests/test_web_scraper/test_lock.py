"""Tests for src/web_scraper/lock.py — process lock with crash detection."""

import os
import sys
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.lock import Lock, LockError  # noqa: E402


def test_acquire_creates_lock(tmp_path):
    lock = Lock(tmp_path)
    state = lock.acquire(cycle=1)
    assert state["pid"] == os.getpid()
    assert state["dirty"] is True
    assert state["cycle"] == 1
    assert (tmp_path / "prometheus.lock").exists()


def test_release_clears_dirty(tmp_path):
    lock = Lock(tmp_path)
    lock.acquire()
    lock.release()
    import json
    state = json.loads((tmp_path / "prometheus.lock").read_text())
    assert state["dirty"] is False


def test_acquire_blocks_second_alive_instance(tmp_path):
    lock1 = Lock(tmp_path)
    lock1.acquire()

    lock2 = Lock(tmp_path)
    try:
        lock2.acquire()
        assert False, "should have raised LockError"
    except LockError:
        pass


def test_acquire_overwrites_stale_dead_pid(tmp_path):
    import json
    dead_pid = 999999
    (tmp_path / "prometheus.lock").write_text(json.dumps({
        "pid": dead_pid,
        "dirty": True,
        "cycle": 5,
        "ts": 0,
    }))

    lock = Lock(tmp_path)
    state = lock.acquire(cycle=6)
    assert state["pid"] == os.getpid()
    assert state["cycle"] == 6


def test_check_and_recover_detects_crash(tmp_path):
    import json
    dead_pid = 999999
    (tmp_path / "prometheus.lock").write_text(json.dumps({
        "pid": dead_pid,
        "dirty": True,
        "cycle": 3,
        "ts": 12345,
    }))

    lock = Lock(tmp_path)
    recovery = lock.check_and_recover()
    assert recovery is not None
    assert recovery["crashed"] is True
    assert recovery["cycle"] == 3


def test_check_and_recover_returns_none_if_clean(tmp_path):
    lock = Lock(tmp_path)
    lock.acquire()
    lock.release()
    assert lock.check_and_recover() is None


def test_check_and_recover_returns_none_if_no_lock(tmp_path):
    lock = Lock(tmp_path)
    assert lock.check_and_recover() is None


def test_check_and_recover_returns_none_if_pid_alive(tmp_path):
    import json
    (tmp_path / "prometheus.lock").write_text(json.dumps({
        "pid": os.getpid(),
        "dirty": True,
        "cycle": 1,
        "ts": 0,
    }))
    lock = Lock(tmp_path)
    assert lock.check_and_recover() is None


def test_release_idempotent(tmp_path):
    lock = Lock(tmp_path)
    lock.acquire()
    lock.release()
    lock.release()
