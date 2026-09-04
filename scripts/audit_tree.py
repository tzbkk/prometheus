#!/usr/bin/env python3
"""实体树审计——逐文件点名违例的只读诊断工具（prometheus 侧工具，不进 zhizong）。

与 scan 的**有意**分工：scan 是宽容遍历（skip+log 保服务启动），审计是点名工具——
逐文件检查、违例逐条报告（file + 类别 + 详情）、汇总非零退出；只报告不修复（零写盘）。

用法::

    .venv/bin/python scripts/audit_tree.py --data-root contracts/fixtures
    .venv/bin/python scripts/audit_tree.py --data-root ./data --guild 7743321643036658

guild 发现：data-root 下任何含 feeds/ 或 comments/ 子树的目录即 guild 根（rglob 发现），
同时兼容运行时布局（<root>/<guild>/）与 fixtures 布局（<root>/data/<guild>/）；
--guild 按目录名过滤，无匹配 → 用法错误。

遍历范围（先实勘后定界）：
- <guild>/feeds/ + <guild>/comments/ 全部文件 → 实体检查（Schema + 写者纪律）；
- <guild>/media/ 全部文件 → 仅文件名检查（64-hex 内容寻址**形态** + 桶位），零二进制读
  （媒体状态内嵌于实体 _p.media；字节级哈希复核不做——fixtures 携带哑载荷，
  名实一致性归写者与迁移工具纪律）；
- 其余文件（如 prometheus.lock、archives/*.tar.zst）→ 范围外跳过：计数 + 逐路径列出，
  不计违例。

违例类别（每文件可多条，逐条点名；根因级联抑制——_p 缺失时不再报 Schema/键序）：
- CORRUPT_JSON       JSON 解析失败或顶层非对象
- _P_MISSING         缺 _p 命名空间（范式 B：每实体文件必有 _p）
- _P_NOT_LAST        _p 不是 JSON 顶层末键（写者纪律：腾讯键序保留、_p 恒末键）
- _P_KEY_ORDER       _p 内键序 ≠ 契约表序（Feed: captured_via,first_seen,last_seen,media；
                     Comment/Reply 多首键 feed_id）——契约不可表达、由本审计兜底（Feed.yaml _p Description）
- ID_MISMATCH        文档 id 与文件名（实体身份）不一致
- GUILD_MISMATCH     feed 顶体 channelInfo.sign.guild_id 与所在 guild 目录不一致（多频道树定根）
- SHARD_MISMATCH     分片路径不正确（错桶/错字面/目录形态畸形——B_{2hex}/c_{2hex}/r_{2hex}）
- MEDIA_NAME_INVALID 媒体文件名违反内容寻址语法 /\\^[0-9a-f]{64}\\.(jpg|png|mp4|gif)$/
- SCHEMA_FAIL        zhizong 当库编译 Schema 校验失败（薄钉集 + _p 全钉 + media 8 字段 + 枚举值域）

Exit codes: 0 = 全绿（含零 guild/零实体的空树）；1 = 存在违例（逐类列出）；
2 = 用法错误（缺 --data-root / data-root 不存在 / --guild 无匹配）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from jsonschema import Draft202012Validator  # noqa: E402
from zhizong.compile import compile_structure  # noqa: E402
from zhizong.loader import load_corpus  # noqa: E402

from src.entity_store.paths import PathFormatError, resolve  # noqa: E402

CONTRACTS_DIR = PROJECT_ROOT / "contracts"

ENTITY_KIND_DIRS = ("feeds", "comments")
_STRUCTURE_BY_KIND = {"feed": "Feed", "comment": "Comment", "reply": "Reply"}
# _p 内键序 = 契约表序（writer.py 构造序逐字；Comment/Reply 多首键 feed_id）
_P_KEY_ORDER = {
    "feed": ("captured_via", "first_seen", "last_seen", "media"),
    "comment": ("feed_id", "captured_via", "first_seen", "last_seen", "media"),
    "reply": ("feed_id", "captured_via", "first_seen", "last_seen", "media"),
}

SCHEMA_FAIL = "SCHEMA_FAIL"
SHARD_MISMATCH = "SHARD_MISMATCH"
P_NOT_LAST = "_P_NOT_LAST"
P_KEY_ORDER = "_P_KEY_ORDER"
P_MISSING = "_P_MISSING"
MEDIA_NAME_INVALID = "MEDIA_NAME_INVALID"
GUILD_MISMATCH = "GUILD_MISMATCH"
ID_MISMATCH = "ID_MISMATCH"
CORRUPT_JSON = "CORRUPT_JSON"

OUT_OF_SCOPE_NOTE = "outside guild entity/media trees (feeds/ comments/ media/)"


@dataclass(frozen=True)
class Violation:
    """单条违例：类别 + data-root 相对路径 + 详情（点名语义的最小单元）。"""

    category: str
    path: Path
    detail: str


@dataclass(frozen=True)
class AuditReport:
    """一次审计的完整产物（纯内存；stdout 呈现归 render()）。"""

    data_root: Path
    guild_roots: tuple[Path, ...]
    entities_checked: int
    media_checked: int
    outside_files: tuple[Path, ...]
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


_validators: dict[str, Draft202012Validator] | None = None


def validators() -> dict[str, Draft202012Validator]:
    """zhizong 当库编译三实体 Schema → Draft202012Validator（进程级缓存一次）。"""
    global _validators
    if _validators is None:
        corpus = load_corpus(CONTRACTS_DIR)
        _validators = {
            name: Draft202012Validator(compile_structure(corpus.structures()[name], corpus))
            for name in _STRUCTURE_BY_KIND.values()
        }
    return _validators


def discover_guild_roots(data_root: Path) -> list[Path]:
    """guild 根 = data-root 下含 feeds/ 或 comments/ 子树的目录（rglob 发现，排序确定）。

    返回 data-root 相对路径（与 Violation.path / _classify 的 rel 同一参照系）。
    """
    roots: set[Path] = set()
    for marker in ENTITY_KIND_DIRS:
        for found in data_root.rglob(marker):
            if found.is_dir():
                roots.add(found.parent.relative_to(data_root))
    return sorted(roots)


def _classify(
    data_root: Path, guild_roots: list[Path]
) -> tuple[list[tuple[Path, str, str]], list[Path], list[Path]]:
    """全树文件三分：实体（rel, kind, guild）/ 媒体 / 范围外（均 data-root 相对路径）。"""
    entities: list[tuple[Path, str, str]] = []
    media: list[Path] = []
    outside: list[Path] = []
    for path in sorted(p for p in data_root.rglob("*") if p.is_file()):
        rel = path.relative_to(data_root)
        placed = False
        for guild_root in guild_roots:
            try:
                tail = rel.relative_to(guild_root)
            except ValueError:
                continue
            head = tail.parts[0]
            if head in ENTITY_KIND_DIRS and len(tail.parts) >= 3:
                kind = {"feeds": "feed", "comments": None}[head]
                if kind is None:  # comments/c_、comments/r_ 由 resolve 逆解析判定
                    kind = "comment" if tail.parts[1].startswith("c_") else "reply"
                entities.append((rel, kind, guild_root.name))
            elif head == "media" and len(tail.parts) >= 3:
                media.append(rel)
            else:
                outside.append(rel)
            placed = True
            break
        if not placed:
            outside.append(rel)
    return entities, media, outside


def _schema_error_path(error: object) -> str:
    parts = ["$"]
    for segment in getattr(error, "absolute_path", ()):  # jsonschema ValidationError
        parts.append(str(segment) if isinstance(segment, int) else f".{segment}")
    return "".join(parts) if len(parts) > 1 else "$"


def _check_entity(abs_path: Path, rel: Path, kind: str, guild: str) -> list[Violation]:
    """单实体逐文件检查（根因级联：CORRUPT_JSON/_P_MISSING 短路后续域检查）。

    abs_path 供磁盘读（IO），rel 供违例点名（data-root 相对参照系）。
    """
    violations: list[Violation] = []
    try:
        doc = json.loads(abs_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return [Violation(CORRUPT_JSON, rel, f"unreadable/corrupt JSON: {exc}")]
    if not isinstance(doc, dict):
        return [Violation(CORRUPT_JSON, rel, f"top level is {type(doc).__name__}, not an object")]

    p = doc.get("_p")
    if not isinstance(p, dict):
        return [Violation(P_MISSING, rel, "lacks the _p namespace (范式 B：每实体文件必有 _p)")]

    if list(doc)[-1] != "_p":
        violations.append(
            Violation(P_NOT_LAST, rel, f"_p is key #{list(doc).index('_p') + 1}, must be last")
        )

    expected_order = _P_KEY_ORDER[kind]
    if list(p) != list(expected_order):
        violations.append(
            Violation(
                P_KEY_ORDER,
                rel,
                f"_p key order {list(p)} != contract table order {list(expected_order)}",
            )
        )

    entity_id = rel.stem
    doc_id = doc.get("id")
    if doc_id is not None and doc_id != entity_id:
        violations.append(
            Violation(ID_MISMATCH, rel, f"document id {doc_id!r} != file name id {entity_id!r}")
        )

    if kind == "feed":
        channel = doc.get("channelInfo")
        sign = channel.get("sign") if isinstance(channel, dict) else None
        gid = sign.get("guild_id") if isinstance(sign, dict) else None
        if isinstance(gid, str) and gid != guild:
            violations.append(
                Violation(GUILD_MISMATCH, rel, f"channelInfo.sign.guild_id {gid!r} != tree {guild!r}")
            )

    errors = sorted(
        validators()[_STRUCTURE_BY_KIND[kind]].iter_errors(doc), key=lambda e: _schema_error_path(e)
    )
    for error in errors:
        violations.append(
            Violation(SCHEMA_FAIL, rel, f"at {_schema_error_path(error)}: {error.message}")
        )
    return violations


def _check_media(abs_path: Path, rel: Path) -> list[Violation]:
    """媒体文件名检查（零二进制读）：64-hex 内容寻址形态 + 桶位 = 名前 2 hex。"""
    try:
        resolve(rel)
    except PathFormatError as exc:
        if "violates contract grammar" in str(exc):
            return [Violation(MEDIA_NAME_INVALID, rel, str(exc))]
        return [Violation(SHARD_MISMATCH, rel, str(exc))]
    return []


def audit_tree(data_root: Path | str, guild: str | None = None) -> AuditReport:
    """审计一棵数据树 → AuditReport（纯内存只读；违例逐条点名）。"""
    root = Path(data_root)
    roots = discover_guild_roots(root)
    if guild is not None:
        roots = [r for r in roots if r.name == guild]

    entities, media, outside = _classify(root, roots)
    violations: list[Violation] = []
    for rel, kind, guild_name in entities:
        try:
            resolve(rel)  # 分片路径三重校验（目录字面/桶形态/桶↔id 一致）
        except PathFormatError as exc:
            violations.append(Violation(SHARD_MISMATCH, rel, str(exc)))
            continue
        violations.extend(_check_entity(root / rel, rel, kind, guild_name))
    for rel in media:
        violations.extend(_check_media(root / rel, rel))

    return AuditReport(
        data_root=root,
        guild_roots=tuple(roots),
        entities_checked=len(entities),
        media_checked=len(media),
        outside_files=tuple(outside),
        violations=tuple(violations),
    )


def render(report: AuditReport) -> str:
    """人读报告（结构分节：发现/范围/违例点名/汇总）。"""
    lines = [
        "=== audit_tree: entity-tree audit (schema + writer discipline) ===",
        f"data root : {report.data_root}",
        f"guild roots ({len(report.guild_roots)}): "
        + ", ".join(str(r) for r in report.guild_roots),
        "scope     : feeds/+comments/ entities (Schema + _p discipline) · "
        "media/ names (64-hex content-address form, zero binary reads)",
        f"skipped (out of scope): {len(report.outside_files)} file(s)",
    ]
    lines.extend(f"  - {rel} — {OUT_OF_SCOPE_NOTE}" for rel in report.outside_files)

    if report.violations:
        lines.append(f"VIOLATIONS ({len(report.violations)}):")
        lines.extend(f"  [{v.category}] {v.path} — {v.detail}" for v in report.violations)
    else:
        lines.append("VIOLATIONS: none")

    per_category = {c: 0 for c in dict.fromkeys(v.category for v in report.violations)}
    for v in report.violations:
        per_category[v.category] += 1
    tally = " ".join(f"{c}={n}" for c, n in per_category.items()) or "-"
    lines.append(
        f"summary: entities {report.entities_checked} · media {report.media_checked} · "
        f"skipped {len(report.outside_files)} · violations {len(report.violations)} ({tally})"
    )
    lines.append(f"RESULT: {'CLEAN — exit 0' if report.ok else 'DIRTY — exit 1'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。exit 0 = 全绿 / 1 = 有违例 / 2 = 用法错误（语义见模块 docstring）。"""
    parser = argparse.ArgumentParser(
        prog="audit_tree.py",
        description="entity-tree audit: per-file schema conformance (zhizong) "
        "+ prometheus writer discipline (_p last key/key order, shard paths, "
        "64-hex content-addressed media names, feed guild rooting). Read-only.",
        epilog="exit codes: 0 clean · 1 violations (listed per category) · 2 usage error",
    )
    parser.add_argument(
        "--data-root", required=True, metavar="DIR",
        help="data tree to audit (guild roots discovered recursively beneath it)",
    )
    parser.add_argument(
        "--guild", default=None, metavar="GUILD_ID",
        help="audit only the guild root whose directory name equals GUILD_ID",
    )
    args = parser.parse_args(argv)

    data_root = Path(args.data_root)
    if not data_root.is_dir():
        print(f"audit_tree: data root does not exist or is not a directory: {data_root}", file=sys.stderr)
        return 2
    if args.guild is not None:
        discovered = {r.name for r in discover_guild_roots(data_root)}
        if args.guild not in discovered:
            print(
                f"audit_tree: --guild {args.guild!r} matches no discovered guild root under {data_root} "
                f"(discovered: {sorted(discovered) or 'none'})",
                file=sys.stderr,
            )
            return 2

    report = audit_tree(data_root, args.guild)
    print(render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
