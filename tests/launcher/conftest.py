"""tests/launcher 套件共享 fixture（launcher 契约面活体）。

- schema_assert：harness 薄胶水同形复制（tests/harness/conftest.py
  同形——服务 fixture 归各测试模块自持）。
- 监督替身 _FakeProc 外缝：Popen.terminate 穿 conftest 信号守卫
  ——活体 API 测试零真子进程。
"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import src.launcher.process_manager as pm_module
from src.launcher.api import LauncherApi
from src.launcher.process_manager import ProcessManager

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"

GUILD = "1000000000000001"


@pytest.fixture(scope="session")
def schema_assert():
    """assert_matches(payload, structure_name)——harness 薄胶水同形复制。"""
    from zhizong.compile import compile_structure
    from zhizong.loader import load_corpus

    corpus = load_corpus(CONTRACTS_DIR)
    cache: dict[str, Draft202012Validator] = {}

    def _assert_matches(payload, structure_name):
        if structure_name not in cache:
            schema = compile_structure(corpus.structures()[structure_name], corpus)
            cache[structure_name] = Draft202012Validator(schema)
        errors = sorted(cache[structure_name].iter_errors(payload), key=lambda e: e.path)
        assert not errors, "{0} violations: {1}".format(
            structure_name, [e.message for e in errors]
        )

    return _assert_matches


@pytest.fixture
def http():
    """http(method, url, payload=None) -> (status, body)——urllib 薄客户端（同形）。"""

    def _request(method, url, payload=None, timeout=5):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read())

    def _parse(raw):
        text = raw.decode("utf-8")
        try:
            return json.loads(text)
        except ValueError:
            return text

    return _request


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


@pytest.fixture
def launcher_service(supervisor, tmp_path):
    """工厂：每个测试自持的活体 LauncherApi（port=0 临时端口）。"""
    made = []

    def _make(**kwargs):
        kwargs.setdefault("process_manager", supervisor)
        kwargs.setdefault("config", {"launcher_port": 9421, "max_restarts": 5})
        kwargs.setdefault("config_path", str(tmp_path / "launcher.conf.json"))
        kwargs.setdefault("guilds", [{"guild_id": GUILD}])
        kwargs.setdefault("port", 0)
        api = LauncherApi(**kwargs)
        api.start()
        made.append(api)
        return api

    yield _make
    for api in made:
        api.stop()
