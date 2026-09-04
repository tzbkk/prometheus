"""时间窗打包引擎（CLI 单入口——scripts/archive.py）。

契约面：contracts/components/archive.yaml + structures/{ArchivePackage,PackageName,
Manifest}.yaml（窗口语义/包语义/exit codes）。

窗口数学：
- 判据 = 实体自身 createTime（业务时钟）∈ (from, to]，from/to = UTC YYYYMMDD；
  _p.first_seen/last_seen 观测时钟与归档窗无关。数据源 = scan.iter_window
  （公共 API 字面消费，窗口迭代器唯一居所——本模块不造第二遍历）。
- from 日 00:00:00 排除、to 日 23:59:59 含入（createTime 为整秒，即整日闭区间）；
  from == to 合法（单日窗）；倒序（from > to）/ 非法日历 / 未来窗（to 日超出
  当前 UTC 日）→ WindowError（调用方转 exit 2）。

包语义：
- 包 = 实体树切片镜像（包内布局 = guild 树布局 feeds/ comments/）+ 媒体并集
  （窗口实体 _p.media 的 file 并集，内容寻址天然去重）+ manifest.json（tar 首
  成员）；tar.zst 流式压缩 + .tmp + os.replace 原子落盘。

对账（数据完整性优先）：
- 打包前：树内每个实体文件要么在窗内（入包），要么可严格证明窗外——宽容遍历
  （iter_window skip+log）会静默丢实体，任何 strict 加载失败/窗外不可证 →
  ReconciliationError（exit 3）。媒体引用 file 缺盘 / 内容 sha256 与文件名
  摘要不符（名实一致）同理 exit 3。
- 打包后：manifest = 实际包内容（同内存快照派生，计数/清单/哈希天然一致；
  解包对账断言归测试面）。

快照语义：entries 在 plan 阶段物化（iter_window 产出全量驻内存）——manifest
与 tar 成员同源同刻，杜绝 plan/write 两趟间的树漂移；内存代价 ≈ 窗内实体
JSON 总量（43k 实体树量级实测秒级扫描）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import zhizong
import zstandard

from src.entity_store.paths import PathFormatError, media_dir
from src.entity_store.reader import ReaderError, created_at_of, load_entity
from src.entity_store.scan import WindowEntry, iter_window

__all__ = [
    "WindowError",
    "ReconciliationError",
    "MediaRef",
    "PackagePlan",
    "parse_ymd",
    "window_bounds",
    "package_stem",
    "plan_package",
    "build_manifest",
    "write_package",
    "list_guilds",
    "list_packages",
]


class WindowError(ValueError):
    """窗参无效（畸形 YYYYMMDD/非法日历/倒序/未来窗）——exit 2。"""


class ReconciliationError(RuntimeError):
    """打包前对账失败（树计数 vs 窗计数不一致/媒体缺盘/名实不符）——exit 3。"""


_YMD_RE = re.compile(r"^[0-9]{8}$")
# PackageName 标量 grammar 逐字（list_packages 过滤面；与契约 productions 恒一致）
_PACKAGE_STEM_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z(?:_full|_from_[0-9]{8}_to_[0-9]{8})$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_READ_CHUNK = 1 << 20
_GUILD_RE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class MediaRef:
    """manifest 媒体清单条目：内容寻址名 + 包内相对路径 + 内容摘要（名实同源）。"""

    name: str
    path: str  # 包内相对路径 media/{shard}/{name}（镜像 guild 树布局）
    sha256: str


@dataclass(frozen=True)
class PackagePlan:
    """plan_package 产物：窗口内实体快照 + 计数 + 媒体并集（manifest 与 tar 同源）。"""

    guild: str
    from_ymd: str
    to_ymd: str
    from_ts: int
    to_ts: int
    guild_root: Path
    entries: tuple[WindowEntry, ...]
    counts: dict[str, int]
    media: tuple[MediaRef, ...]

    @property
    def is_empty(self) -> bool:
        """空窗（三类计数皆零；媒体派生于实体，恒随零）——不落包。"""
        return sum(self.counts.values()) == 0


# ---- 窗口数学 -----------------------------------------------------------------


def parse_ymd(value: object, label: str) -> datetime:
    """YYYYMMDD 八位数字串 → 该日 UTC 零点；畸形/非法日历 → WindowError。"""
    if not isinstance(value, str) or not _YMD_RE.match(value):
        raise WindowError(f"{label} must be a YYYYMMDD string, got {value!r}")
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise WindowError(f"{label} is not a valid calendar date: {value!r}") from exc
    return parsed.replace(tzinfo=timezone.utc)


def window_bounds(from_ymd: str, to_ymd: str, *, now_ts: float | None = None) -> tuple[int, int]:
    """(from_ts, to_ts] 整秒窗界 + 校验（倒序/未来窗 → WindowError）。

    to_ts = to 日 23:59:59（createTime 整秒域的整日闭端）；from == to 合法（单日窗）。
    now_ts 可注入（测试确定性）；缺省读墙钟（唯一时钟读取点）。
    """
    from_dt = parse_ymd(from_ymd, "from")
    to_dt = parse_ymd(to_ymd, "to")
    if from_dt > to_dt:
        raise WindowError(
            f"inverted window: from {from_ymd!r} is later than to {to_ymd!r}"
            " (window is half-open (from, to])"
        )
    now = time.time() if now_ts is None else now_ts
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    today_end = (
        datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
        + timedelta(days=1)
        - timedelta(seconds=1)
    )
    to_end = to_dt + timedelta(days=1) - timedelta(seconds=1)
    if to_end > today_end:
        raise WindowError(
            f"future window: to {to_ymd!r} lies beyond the current UTC date"
            f" {now_dt.strftime('%Y%m%d')}"
        )
    from_ts = int(from_dt.timestamp())
    to_ts = int(to_dt.timestamp()) + 86400 - 1
    return from_ts, to_ts


# ---- 枚举面（guilds/packages）------------------------------------------------


def list_guilds(data_root: Path | str) -> list[str]:
    """数据根下含 feeds/ 或 comments/ 子树的数字目录（GuildId grammar；升序）。"""
    root = Path(data_root)
    if not root.is_dir():
        return []
    guilds = [
        entry.name
        for entry in root.iterdir()
        if _GUILD_RE.match(entry.name)
        and entry.is_dir()
        and ((entry / "feeds").is_dir() or (entry / "comments").is_dir())
    ]
    return sorted(guilds)


def list_packages(archives_dir: Path | str, guild: str | None = None) -> list[str]:
    """已有归档包名主干（PackageName grammar 过滤；创建时刻降序 = 名降序）。

    guild 给定 → 仅该频道（archives/{guild}/packages/）；None → 全频道并集。
    未知 guild / 空 archives 根 → []（fresh install 语义）。
    """
    root = Path(archives_dir)
    if not root.is_dir():
        return []
    if guild is not None:
        package_dirs = [root / guild / "packages"]
    else:
        package_dirs = [
            entry / "packages" for entry in root.iterdir() if entry.is_dir()
        ]
    stems: list[str] = []
    for directory in package_dirs:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file() or path.suffixes[-2:] != [".tar", ".zst"]:
                continue
            stem = path.name[: -len(".tar.zst")]
            if _PACKAGE_STEM_RE.match(stem):
                stems.append(stem)
    return sorted(stems, reverse=True)


# ---- 打包计划 + 对账 ---------------------------------------------------------


def _entity_files(guild_root: Path) -> list[Path]:
    """guild 树 feeds/ + comments/ 全部实体文件（升序；media/ 二进制树不历）。

    排除 *.tmp——写者崩溃残留是纪律内可接受 artifact（原子写惯例），
    非数据损坏，不应阻塞打包；其余任何非实体文件由对账面 fail loud。
    """
    files: list[Path] = []
    for kind_dir in ("feeds", "comments"):
        directory = guild_root / kind_dir
        if directory.is_dir():
            files.extend(
                p for p in directory.rglob("*") if p.is_file() and p.suffix != ".tmp"
            )
    return sorted(files)


def _reconcile_tree(
    guild_root: Path, entries: tuple[WindowEntry, ...], from_ts: int, to_ts: int
) -> None:
    """打包前对账：树内每文件 ∈ 窗内快照 或 可严格证窗外；否则 exit 3。

    iter_window 是宽容遍历（损坏 skip+log）——静默丢实体即计数不一致。本 pass
    对窗外的每个文件走严格链（resolve → load_entity → id 一致 → createTime），
    任一环失败 = 宽容遍历曾静默跳过 = 包将失真 → ReconciliationError。
    """
    window_paths = {entry.path for entry in entries}
    for path in _entity_files(guild_root):
        if path in window_paths:
            continue
        try:
            doc = load_entity(path)
        except (ReaderError, OSError) as exc:
            raise ReconciliationError(
                f"count reconciliation failed: unreadable entity {path}: {exc}"
            ) from exc
        try:
            created = created_at_of(doc)
        except ReaderError as exc:
            raise ReconciliationError(
                f"count reconciliation failed: malformed createTime in {path}: {exc}"
            ) from exc
        if from_ts < created <= to_ts:
            raise ReconciliationError(
                f"count reconciliation failed: in-window entity missing from the"
                f" window pass: {path}"
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _media_union(
    entries: tuple[WindowEntry, ...], data_root: Path, guild: str
) -> tuple[MediaRef, ...]:
    """窗口实体 _p.media 的 file 并集（内容寻址去重为单条目）。

    每文件实算内容 sha256（manifest 清单所需）并复核名实一致（文件名摘要段 ==
    内容摘要）——引用缺盘/名实不符/文件名违 grammar → ReconciliationError
    （exit 3，数据完整性优先）。按名升序保确定性。
    """
    seen: dict[str, MediaRef] = {}
    for entry in entries:
        media = entry.doc.get("_p", {}).get("media", [])
        for item in media:
            if not isinstance(item, dict):
                continue
            name = item.get("file")
            if not isinstance(name, str) or not name:
                continue  # pending（file=null）——无盘上物，不入包
            if name in seen:
                continue
            try:
                disk_path = media_dir(data_root, guild, name) / name
            except PathFormatError as exc:
                raise ReconciliationError(
                    f"media reconciliation failed: {name!r} violates the"
                    f" content-addressed grammar: {exc}"
                ) from exc
            if not disk_path.is_file():
                raise ReconciliationError(
                    f"media reconciliation failed: referenced media missing on"
                    f" disk: {disk_path}"
                )
            digest = _sha256_file(disk_path)
            if not name.startswith(digest + "."):
                raise ReconciliationError(
                    f"media reconciliation failed: name/content mismatch for"
                    f" {disk_path} (content sha256 {digest})"
                )
            seen[name] = MediaRef(
                name=name, path=f"media/{name[:2]}/{name}", sha256=digest
            )
    return tuple(seen[name] for name in sorted(seen))


def plan_package(
    data_root: Path | str,
    guild: str,
    from_ymd: str,
    to_ymd: str,
    *,
    now_ts: float | None = None,
) -> PackagePlan:
    """窗内快照 + 打包前对账（WindowError → exit 2；ReconciliationError → exit 3）。

    guild 非法 → ValueError；guild 目录缺失 → FileNotFoundError（皆归 exit 1 面，
    服务侧 POST 已先行 404）。
    """
    if not isinstance(guild, str) or not _GUILD_RE.match(guild):
        raise ValueError(f"guild must be a decimal digit string, got {guild!r}")
    from_ts, to_ts = window_bounds(from_ymd, to_ymd, now_ts=now_ts)
    root = Path(data_root)
    guild_root = root / guild
    if not guild_root.is_dir():
        raise FileNotFoundError(f"guild directory not found: {guild_root}")

    entries = tuple(iter_window(root, guild, from_ts, to_ts))
    _reconcile_tree(guild_root, entries, from_ts, to_ts)
    counts = {"feeds": 0, "comments": 0, "replies": 0}
    for entry in entries:
        counts[f"{entry.kind}s" if entry.kind != "reply" else "replies"] += 1
    media = _media_union(entries, root, guild)
    return PackagePlan(
        guild=guild,
        from_ymd=from_ymd,
        to_ymd=to_ymd,
        from_ts=from_ts,
        to_ts=to_ts,
        guild_root=guild_root,
        entries=entries,
        counts=counts,
        media=media,
    )


# ---- 包写出 ------------------------------------------------------------------


def package_stem(from_ymd: str, to_ymd: str, created_ms: int) -> str:
    """PackageName grammar 增量形：<创建时刻>_from_<from>_to_<to>。"""
    stamp = (_EPOCH + timedelta(milliseconds=created_ms)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_from_{from_ymd}_to_{to_ymd}"


def build_manifest(plan: PackagePlan, created_ms: int, level: int) -> dict:
    """manifest.json 体（薄钉六要素 + 工程宽容键；契约 structures/Manifest.yaml）。"""
    created = _EPOCH + timedelta(milliseconds=created_ms)
    return {
        "format_version": 2,
        "guild_id": plan.guild,
        "window": {"from": plan.from_ymd, "to": plan.to_ymd},
        "counts": dict(plan.counts),
        "media": [
            {"path": ref.path, "sha256": ref.sha256} for ref in plan.media
        ],
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zhizong_version": zhizong.__version__,
        "compression": {
            "format": "tar.zst",
            "level": level,
            "zstandard_version": zstandard.__version__,
        },
    }


def _tar_add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes, mtime: int) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = mtime
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _tar_add_file(tar: tarfile.TarFile, path: Path, arcname: str, mtime: int) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = path.stat().st_size
    info.mtime = mtime
    info.mode = 0o644
    with open(path, "rb") as fh:
        tar.addfile(info, fh)


def write_package(
    plan: PackagePlan,
    archives_dir: Path | str,
    *,
    level: int = 7,
    force: bool = False,
    created_ms: int | None = None,
) -> Path:
    """写 tar.zst 包（manifest.json 首成员 → 实体镜像 → 媒体并集；原子落盘）。

    流式形态：ZstdCompressor.stream_writer → tarfile "w|"；
    .tmp + os.replace 原子替换；已存在且未 --force → RuntimeError（exit 1 面）。
    所有成员 mtime 钉 created（确定性包，不泄写者 uid/gid/mtime）。
    """
    if created_ms is None:
        created_ms = int(time.time() * 1000)
    stem = package_stem(plan.from_ymd, plan.to_ymd, created_ms)
    out_dir = Path(archives_dir) / plan.guild / "packages"
    out_path = out_dir / f"{stem}.tar.zst"
    if out_path.exists() and not force:
        raise RuntimeError(
            f"output package already exists: {out_path} (use --force to overwrite)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(plan, created_ms, level)
    mtime = created_ms // 1000
    compressor_ctx = zstandard.ZstdCompressor(
        level=level, threads=os.cpu_count() or 0
    )
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    with open(tmp_path, "wb") as out_fh:
        with compressor_ctx.stream_writer(out_fh) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tar:
                data = (
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8")
                _tar_add_bytes(tar, "manifest.json", data, mtime)
                for entry in plan.entries:
                    arcname = str(entry.path.relative_to(plan.guild_root))
                    _tar_add_file(tar, entry.path, arcname, mtime)
                for ref in plan.media:
                    _tar_add_file(tar, plan.guild_root / ref.path, ref.path, mtime)
    os.replace(tmp_path, out_path)
    return out_path
