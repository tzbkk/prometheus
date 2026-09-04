"""launcher 客户端/守护分离：

- ``LauncherClient``：动词→HTTP 映射 + 错误信封转译（伪 transport 单测）；
- ``Dispatcher(remote=...)``：status/start/stop/restart/config/quit 远程化；
- 守护进程真演练：``--daemon`` 子进程起动 → 两个客户端连接看到同一
  TargetList（新开的 shell 必须探测到在跑的目标）。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time

import pytest

from src.launcher.client import LauncherClient, LauncherClientError
from src.launcher.commands import Cmd, Dispatcher

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# 守卫逃生门（tests/conftest.py no_real_signals 指定形态）：模块导入期
# （守卫尚未挂钩）留真 os.kill 底本，真子进程演练在测试内显式还原。
_OS_KILL_ORIGINAL = os.kill

_TARGETS_STUB = {
    "targets": [
        {"name": "scraper", "state": "running", "pid": 111, "uptime_sec": 5,
         "restarts": 0},
        {"name": "deepbackfill", "state": "running", "pid": 222, "uptime_sec": 9,
         "restarts": 1},
        {"name": "viewer", "state": "stopped", "pid": None, "uptime_sec": 0,
         "restarts": 0},
    ]
}


class _FakeTransport:
    def __init__(self, routes=None, fail=False):
        self.calls = []
        self.routes = routes or {}
        self.fail = fail

    def __call__(self, method, url, body):
        self.calls.append((method, url.split(":9421")[-1], body))
        if self.fail:
            raise OSError("connection refused")
        path = url.split(":9421")[-1]
        for (m, p), (status, payload) in self.routes.items():
            if m == method and p == path:
                return status, payload
        return 404, {"error": {"code": "not_found", "message": "no route"}}


def _client(transport):
    return LauncherClient(9421, request=transport)


def test_client_verb_http_mapping_and_error_envelope():
    """MD-131：start/stop/restart→POST /targets/{t}/…，config set→PUT，
    shutdown→POST /shutdown；4xx 回 ErrorEnvelope → LauncherClientError
    带 daemon message；不可达 → alive()=False（不 raise）。"""
    t = _FakeTransport(routes={
        ("POST", "/targets/viewer/start"): (200, _TARGETS_STUB["targets"][2]),
        ("POST", "/shutdown"): (200, {"accepted": True}),
        ("PUT", "/config"): (200, {"launcher_port": 9421, "k": 1}),
        ("POST", "/targets/viewer/stop"): (409, {
            "error": {"code": "busy", "message": "stop in flight"}}),
    })
    c = _client(t)
    assert c.start("viewer")["name"] == "viewer"
    assert c.shutdown() == {"accepted": True}
    assert c.config_set("k", 1)["k"] == 1
    with pytest.raises(LauncherClientError, match="stop in flight"):
        c.stop("viewer")
    assert t.calls[0][0:2] == ("POST", "/targets/viewer/start")
    dead = _client(_FakeTransport(fail=True))
    assert dead.alive() is False
    with pytest.raises(LauncherClientError, match="unreachable"):
        dead.status_all()


def test_dispatcher_remote_mode_remaps_verbs(monkeypatch):
    """MD-132：remote 模式 status/start/stop/restart/config show 全走
    LauncherClient（零 pm 触碰——pm=None 亦不炸）；幂等双启消息与宿主
    模式逐字；quit 消息 = targets keep running；shutdown 动词 →
    shutdown_tree 动作。"""
    t = _FakeTransport(routes={
        ("GET", "/targets"): (200, _TARGETS_STUB),
        ("GET", "/targets/scraper"): (200, _TARGETS_STUB["targets"][0]),
        ("GET", "/targets/viewer"): (200, _TARGETS_STUB["targets"][2]),
        ("GET", "/targets/deepbackfill"): (200, _TARGETS_STUB["targets"][1]),
        ("POST", "/targets/viewer/start"): (200, _TARGETS_STUB["targets"][2]),
        ("GET", "/config"): (200, {"launcher_port": 9421}),
    })
    d = Dispatcher(None, {}, "conf/launcher.conf.json", remote=_client(t))

    monkeypatch.setattr(d, "_probe_http", lambda port, path, timeout=3.0: True)
    r = d.dispatch(Cmd(verb="health", noun=None, args=[]))
    assert r["ok"] is False  # viewer stopped → 整体不 OK
    assert "deepbackfill :9424  running  restarts 1  API OK" in r["message"]
    assert "viewer       :9422  stopped  restarts 0" in r["message"]

    r = d.dispatch(Cmd(verb="start", noun="viewer", args=[]))
    assert r["ok"] and r["message"] == "viewer started."
    r = d.dispatch(Cmd(verb="start", noun="deepbackfill", args=[]))
    assert not r["ok"] and r["message"] == "deepbackfill is already running."

    r = d.dispatch(Cmd(verb="config", noun="show", args=[]))
    assert "launcher" in r["message"]
    assert "launcher_port" in r["message"]

    r = d.dispatch(Cmd(verb="quit", noun=None, args=[]))
    assert "keep running" in r["message"]
    r = d.dispatch(Cmd(verb="shutdown", noun=None, args=[]))
    assert r["data"]["action"] == "shutdown_tree"


def test_daemon_boots_and_two_clients_see_same_tree(monkeypatch):
    """MD-133：--daemon 子进程起动 → :94xx 探活 → 两个独立客户端看到
    同一 TargetList（新开的 launcher 必须探测到）；SIGTERM
    → 干净退出（rc=0，端口释放）。"""
    monkeypatch.setattr(os, "kill", _OS_KILL_ORIGINAL)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = subprocess.Popen(
        [".venv/bin/python", "-m", "src.launcher", "--daemon",
         "--port", str(port)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        a = LauncherClient(port)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not a.alive():
            assert p.poll() is None, p.stdout.read().decode()[-400:]
            time.sleep(0.25)
        names_a = [x["name"] for x in a.status_all()["targets"]]
        b = LauncherClient(port)
        assert [x["name"] for x in b.status_all()["targets"]] == names_a
        assert set(names_a) == {"scraper", "deepbackfill", "viewer"}
        assert all(x["state"] == "stopped" for x in a.status_all()["targets"])
    finally:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                rc = p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                rc = "killed"
        else:
            rc = p.returncode
    assert rc == 0
    time.sleep(0.3)
    assert LauncherClient(port).alive() is False
