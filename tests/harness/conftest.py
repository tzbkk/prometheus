"""tests/harness — 共享活体 fixture。

形态纪律（Must NOT：fixture 库化/抽象层）：本文件只有薄胶水——
一个 urllib 客户端 fixture + 一个 Schema 断言 fixture；活体服务 fixture
归各服务测试模块自持（每服务一模块）。

可迁移注记：schema_assert 是"zhizong 编译 + jsonschema 校验"的单函数
包装，与断言逻辑零耦合——zhizong IO 校验器落地后
此 fixture 整体迁出（消费测试面一行不改）。jsonschema 依赖经
[contracts] extra → zhizong 供给（CI test job 已装）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"


@pytest.fixture(scope="session")
def compiled_corpus():
    """zhizong 语料（session 级加载一次）。"""
    from zhizong.loader import load_corpus

    return load_corpus(CONTRACTS_DIR)


@pytest.fixture(scope="session")
def schema_assert(compiled_corpus):
    """assert_matches(payload, structure_name)——薄胶水重编译 Schema。

    惰性编译缓存 per structure；违例即 assert 失败并列出全部违规路径。
    """
    from zhizong.compile import compile_structure

    cache: dict[str, Draft202012Validator] = {}

    def _assert_matches(payload, structure_name):
        if structure_name not in cache:
            schema = compile_structure(
                compiled_corpus.structures()[structure_name], compiled_corpus
            )
            cache[structure_name] = Draft202012Validator(schema)
        errors = sorted(cache[structure_name].iter_errors(payload), key=lambda e: e.path)
        assert not errors, "{0} violations: {1}".format(
            structure_name, [e.message for e in errors]
        )

    return _assert_matches


@pytest.fixture
def http():
    """http(method, url, payload=None) -> (status, body)——urllib 薄客户端。

    HTTPError 归一为 (code, parsed body)；JSON 解析失败时 body 为原始 str。
    """

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
