"""entity_store 套件共享 conformance fixture（pillar 对应面）。

zhizong 当库编译三实体 Schema，module 级缓存（编译一次，测试面零重复成本）。

可迁移注记：本 fixture 是"薄胶水重 Schema"形态——compile_structure(doc, corpus)
调用与断言逻辑零耦合；zhizong IO 校验器落地后此块整体迁入，
消费它的测试面一行不改。jsonschema 依赖经 [contracts] extra → zhizong 供给
（CI test job 已装 extra）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
ENTITY_STRUCTURES = ("Feed", "Comment", "Reply")


@pytest.fixture(scope="module")
def compiled_schemas() -> dict[str, dict]:
    """{"Feed": schema, "Comment": schema, "Reply": schema}——zhizong 编译产物（JSON Schema dict）。"""
    from zhizong.compile import compile_structure
    from zhizong.loader import load_corpus

    corpus = load_corpus(CONTRACTS_DIR)
    return {
        name: compile_structure(corpus.structures()[name], corpus)
        for name in ENTITY_STRUCTURES
    }
