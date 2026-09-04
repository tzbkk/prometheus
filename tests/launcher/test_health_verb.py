"""launcher `health` 动词——监督态 + 探测合并面（MD-140..145）。

规格（commands.py Dispatcher._handle_health / CommandParser）：
- ``health`` 裸 = 三目标逐行（scraper/deepbackfill/viewer）；
  每行 = 监督态 + restart 计数（原 ``status`` 动词已退役并入）；
- ``health <target>`` 单目标；非法目标/多余 token/``status`` → 解析期报错；
- 目标未运行 → 到监督态为止（不探测、不等超时）；
- 运行中 → 追加 localhost 端口探测（scraper/deepbackfill /health，
  viewer /api/stats——viewer 无 /health，SPA fallback 恒 200 不可用）；
- ``ok`` = 所有被检目标皆 OK。
"""

from __future__ import annotations

import pytest

from src.launcher.commands import (
    CommandParser,
    Dispatcher,
    InvalidTargetError,
    MissingArgumentError,
)


class _FakePm:
    """宿主模式监督替身——status_of 只回状态行。"""

    def __init__(self, states):
        self._states = states

    def status_of(self, name):
        row = self._states.get(name, "stopped")
        if isinstance(row, str):
            row = {"state": row, "restarts": 0}
        return row


def _dispatcher(states, config=None):
    return Dispatcher(
        pm=_FakePm(states), config=config or {}, config_path=None
    )


# ---- 解析面 ----

def test_health_verb_parses_bare_and_targeted():
    cmd = CommandParser().parse("health")
    assert cmd.verb == "health" and cmd.noun is None

    cmd = CommandParser().parse("health deepbackfill")
    assert cmd.verb == "health" and cmd.noun == "deepbackfill"


def test_health_verb_rejects_invalid_and_extra_tokens():
    with pytest.raises(InvalidTargetError):
        CommandParser().parse("health bogus")
    with pytest.raises(MissingArgumentError):
        CommandParser().parse("health scraper viewer")



# ---- 派发面 ----

def test_health_bare_reports_all_three_lines_with_state(monkeypatch):
    """裸 health：三行；stopped 目标报态不探测（probe 零调用）。"""
    d = _dispatcher({"scraper": {"state": "stopped", "restarts": 2},
                     "deepbackfill": {"state": "running", "restarts": 1},
                     "viewer": {"state": "running", "restarts": 0}})
    probed = []
    monkeypatch.setattr(
        d, "_probe_http",
        lambda port, path, timeout=3.0: probed.append((port, path)) or True,
    )

    res = d.dispatch(CommandParser().parse("health"))

    assert res["ok"] is False
    lines = res["message"].splitlines()
    assert len(lines) == 3
    assert lines[0] == "scraper      :9420  stopped  restarts 2"
    assert lines[1] == "deepbackfill :9424  running  restarts 1  API OK"
    assert lines[2] == "viewer       :9422  running  restarts 0  API OK"
    assert sorted(probed) == [(9422, "/api/stats"), (9424, "/health")]


def test_health_single_target_running_probe_ok(monkeypatch):
    d = _dispatcher({"scraper": "running"}, config={"scraper_api_port": 9430})
    monkeypatch.setattr(d, "_probe_http", lambda port, path, timeout=3.0:
                        (port, path) == (9430, "/health"))

    res = d.dispatch(CommandParser().parse("health scraper"))

    assert res["ok"] is True
    assert res["message"] == "scraper      :9430  running  restarts 0  API OK"


def test_health_single_target_running_probe_timeout(monkeypatch):
    d = _dispatcher({"viewer": "running"})
    monkeypatch.setattr(d, "_probe_http", lambda port, path, timeout=3.0: False)

    res = d.dispatch(CommandParser().parse("health viewer"))

    assert res["ok"] is False
    assert res["message"] == "viewer       :9422  running  restarts 0  API TIMEOUT"


def test_health_single_target_stopped_no_probe(monkeypatch):
    d = _dispatcher({"deepbackfill": "stopped"})
    monkeypatch.setattr(
        d, "_probe_http",
        lambda *a, **kw: pytest.fail("stopped 目标不得探测"),
    )

    res = d.dispatch(CommandParser().parse("health deepbackfill"))

    assert res["ok"] is False
    assert res["message"] == "deepbackfill :9424  stopped  restarts 0"
