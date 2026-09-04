#!/usr/bin/env python3
"""Mandate gate：pytest 收集 node ID ↔ tests/manifest.yaml 严格双向对等。

Node ID 映射规则（机械匹配，零自由度）：
    mandate.suite = "a/b/c"        →  测试文件 tests/a/b/test_c.py
    mandate.name                   →  测试函数 test_<name>（§10.2 登记册示例形态：
                                      name 不含 test_ 前缀，前缀由本规则补齐，
                                      对应关系逐字、无参数化余地）
    推导 node ID                   →  tests/a/b/test_c.py::test_<name>

收集集必须与推导集完全相等（双向零孤儿）：
    - 孤儿测试：收集到但 manifest 无对应 mandate → 红
    - 空 mandate：manifest 登记但无实现 → 红
    - 重复 id / 重复 name / 缺必填字段 / 非法标识符 → 红（登记册本身违规）

禁 parametrize 的理由：parametrize 把一个函数展开成 N 个带 [param] 后缀的
node ID，name ↔ node ID 的双射被破坏（一个 mandate 无法对应 N 个实例，孤儿
判定失真）。同理禁测试类（File::Class::func 两级 node ID）——平铺函数是
node ID 与 mandate 行一一对应的前提。任何含 "[" 或两级 "::" 的收集结果直接
判违规，不给绕行。

用法：python scripts/test_gate.py   （CI test job 中必须先于 pytest 运行）
Exit code: 0 = 双向对等；1 = 任何违规。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "tests" / "manifest.yaml"

REQUIRED_KEYS = ("id", "suite", "name", "asserts", "implements", "pillar")
VALID_PILLARS = ("contract", "format", "behavior", "smoke")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SUITE_SEG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
NODE_RE = re.compile(r"^(tests/(?:[a-z0-9_]+/)*test_[a-z0-9_]+\.py)::test_([a-z][a-z0-9_]*)$")


def die(message: str) -> None:
    print(f"gate: FAIL — {message}", file=sys.stderr)
    raise SystemExit(1)


def load_manifest() -> dict[str, dict]:
    try:
        doc = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"manifest not found: {MANIFEST_PATH}")
    except yaml.YAMLError as exc:
        die(f"manifest is not valid YAML: {exc}")
    if not isinstance(doc, dict) or doc.get("version") != 1:
        die("manifest must be a mapping with version: 1")
    mandates = doc.get("mandates")
    if not isinstance(mandates, list) or not mandates:
        die("manifest.mandates must be a non-empty list")

    by_name: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for index, entry in enumerate(mandates):
        where = f"mandates[{index}]"
        if not isinstance(entry, dict):
            die(f"{where}: entry must be a mapping")
        for key in REQUIRED_KEYS:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                die(f"{where}: missing or empty required key '{key}'")
        if entry["pillar"] not in VALID_PILLARS:
            die(f"{where}: pillar must be one of {VALID_PILLARS}, got {entry['pillar']!r}")
        if entry["id"] in seen_ids:
            die(f"{where}: duplicate id {entry['id']}")
        seen_ids.add(entry["id"])
        name = entry["name"]
        if not NAME_RE.match(name):
            die(f"{where}: name must be snake_case ([a-z][a-z0-9_]*), got {name!r}")
        if name in by_name:
            die(f"{where}: duplicate name {name}")
        for segment in entry["suite"].split("/"):
            if not SUITE_SEG_RE.match(segment):
                die(f"{where}: suite segment {segment!r} must be [a-z][a-z0-9_]*")
        by_name[name] = entry
    return by_name


def expected_node_ids(by_name: dict[str, dict]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for name, entry in by_name.items():
        parts = entry["suite"].split("/")
        relative = "/".join(parts[:-1] + [f"test_{parts[-1]}.py"])
        expected[f"tests/{relative}::test_{name}"] = entry["id"]
    return expected


def collect_node_ids() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 5):
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        die(f"pytest collection failed (exit {proc.returncode})")
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    ]


def validate_collected(nodes: list[str]) -> list[str]:
    clean: list[str] = []
    for node in nodes:
        if "[" in node:
            die(f"parametrized test breaks the name↔node bijection (禁 parametrize): {node}")
        if NODE_RE.match(node) is None:
            die(f"node ID not in flat <file>::test_<name> form (禁测试类/收集器): {node}")
        clean.append(node)
    return clean


def main() -> int:
    by_name = load_manifest()
    expected = expected_node_ids(by_name)
    collected = validate_collected(collect_node_ids())

    orphan_tests = sorted(set(collected) - set(expected))
    empty_mandates = sorted(set(expected) - set(collected))

    print(f"manifest mandates: {len(by_name)}")
    print(f"pytest collected : {len(collected)}")
    if orphan_tests:
        print("\nORPHAN TESTS (collected but not mandated):")
        for node in orphan_tests:
            print(f"  - {node}")
    if empty_mandates:
        print("\nEMPTY MANDATES (registered but not implemented):")
        for node in empty_mandates:
            print(f"  - {node}  [{expected[node]}]")
    if orphan_tests or empty_mandates:
        print("\ngate: FAIL — bidirectional bijection violated ")
        return 1
    print("gate: OK — bidirectional zero orphans")
    return 0


if __name__ == "__main__":
    sys.exit(main())
