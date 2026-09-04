"""Launcher API 存活烟雾——真 HTTP 环回、port=0 临时端口。

响应形 = 裸状态码 + 契约结构体 / ErrorEnvelope（㉒-d）；schema 编译断言
归 tests/launcher/ 套件（pillar 对应面）。"""

import json
import urllib.error
import urllib.request

import pytest

from src.launcher.api import LauncherApi
from src.launcher.process_manager import ProcessManager

TARGETS = ("scraper", "deepbackfill", "viewer")


@pytest.fixture
def live_api():
    api = LauncherApi(ProcessManager(config={}), port=0)
    api.start()
    try:
        yield api
    finally:
        api.stop()


def test_launcher_api_serves_targets_snapshot_on_live_port(live_api):
    assert isinstance(live_api.port, int) and live_api.port > 0
    with urllib.request.urlopen(
        f"http://127.0.0.1:{live_api.port}/targets", timeout=5
    ) as resp:
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
    assert [t["name"] for t in body["targets"]] == list(TARGETS)
    for target in body["targets"]:
        assert target["state"] == "stopped"
        assert target["pid"] is None
        assert target["uptime_sec"] is None
        assert target["restarts"] == 0


def test_launcher_api_returns_404_error_envelope_for_unknown_route(live_api):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"http://127.0.0.1:{live_api.port}/nope", timeout=5
        )
    assert excinfo.value.code == 404
    body = json.loads(excinfo.value.read().decode("utf-8"))
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
