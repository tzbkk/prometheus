"""三目标监督冒烟（：scraper/deepbackfill/viewer）。

外层缝替身：subprocess.Popen 换 _FakeProc、os.getpgid 恒 ProcessLookupError——
断言对象是监督机制本身（进程登记/状态迁移/停止清理/富状态对象），信号投递
不在烟雾层（Popen.terminate 穿 conftest 信号守卫）。
"""

import itertools

import pytest

import src.launcher.process_manager as pm_module
from src.launcher.process_manager import TARGETS, ProcessManager


class _FakeProc:
    """Popen 替身：保留 poll/terminate/wait 状态机语义，零真进程。"""

    def __init__(self, pid):
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.terminated = True

    def wait(self, timeout=None):
        self.terminated = True
        return 0


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    manager = ProcessManager(config={})
    manager.project_root = str(tmp_path)
    pids = itertools.count(100_000)

    def fake_popen(args, **kwargs):
        return _FakeProc(next(pids))

    def no_pgid(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(pm_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(pm_module.os, "getpgid", no_pgid)
    return manager


def _assert_cycle(manager, target):
    manager.start(target)
    assert manager.status_of(target)["state"] == "running"
    manager.stop(target)
    assert manager.status_of(target)["state"] == "stopped"
    assert target not in manager.processes


def test_supervisor_runs_start_stop_cycle_for_scraper(supervisor):
    _assert_cycle(supervisor, "scraper")


def test_supervisor_runs_start_stop_cycle_for_deepbackfill(supervisor):
    _assert_cycle(supervisor, "deepbackfill")


def test_supervisor_runs_start_stop_cycle_for_viewer(supervisor):
    _assert_cycle(supervisor, "viewer")


def test_supervisor_status_covers_three_targets_with_restart_counts(supervisor):
    snapshot = supervisor.status_all()
    assert [t["name"] for t in snapshot["targets"]] == list(TARGETS)
    for target in snapshot["targets"]:
        assert target["state"] == "stopped"
        assert target["restarts"] == 0
        assert target["pid"] is None
        assert target["uptime_sec"] is None
    assert set(supervisor.restart_counts) == set(TARGETS)
    assert all(count == 0 for count in supervisor.restart_counts.values())


def test_supervisor_crashed_process_reports_failed_state(supervisor):
    supervisor.start("scraper")
    supervisor.processes["scraper"]["proc"].terminate()
    status = supervisor.status_of("scraper")
    assert status["state"] == "failed"
    assert status["pid"] is None
    assert status["uptime_sec"] is None
