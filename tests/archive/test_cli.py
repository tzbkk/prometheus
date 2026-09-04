"""CLI 通道 mandate（scripts/archive.py 薄壳同引擎）——exit codes 0/2/3/1。

MD-062（pillar 对应面）：dry-run 不落包 0；--apply 落包 0（PackageName grammar）；
已存在未 --force → 1（time 钉死保确定性）；窗参无效三形态 → 2；
计数不一致树 → 3；未知 guild → 1。
"""

from __future__ import annotations

import re

from scripts.archive import main

from tests.archive.conftest import (
    COMMENT_1,
    GUILD,
    build_window_tree,
)

PACKAGE_STEM_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z(_full|_from_[0-9]{8}_to_[0-9]{8})$"
)


def _run(data_root, archives, *extra, guild=GUILD, from_ymd="20230601", to_ymd="20230602"):
    return main([
        "--guild", guild,
        "--from", from_ymd,
        "--to", to_ymd,
        "--data-root", str(data_root),
        "--output", str(archives),
        *extra,
    ])


def test_cli_exit_codes_window_dry_run_and_apply(data_root, tmp_path, monkeypatch):
    build_window_tree(data_root)
    archives = tmp_path / "archives"
    monkeypatch.setattr("src.archive.engine.time.time", lambda: 1690000000.0)

    # dry-run：窗内计数打印、零落包（默认通道）
    assert _run(data_root, archives) == 0
    assert not archives.exists()

    # 窗参无效（exit 2）：非法日历 / 倒序 / 未来窗 / 非 8 位
    assert _run(data_root, archives, from_ymd="99999999") == 2
    assert _run(data_root, archives, from_ymd="20230602", to_ymd="20230601") == 2
    assert _run(data_root, archives, from_ymd="20990101", to_ymd="20991231") == 2
    assert _run(data_root, archives, from_ymd="2023-6-1") == 2

    # 未知 guild → 1
    assert _run(data_root, archives, guild="9999999999999999") == 1

    # apply：包落 {output}/{guild}/packages/，名 = PackageName grammar
    assert _run(data_root, archives, "--apply") == 0
    packages = list((archives / GUILD / "packages").glob("*.tar.zst"))
    assert len(packages) == 1
    assert PACKAGE_STEM_RE.match(packages[0].name[: -len(".tar.zst")])
    assert packages[0].name == "20230722T042640Z_from_20230601_to_20230602.tar.zst"

    # 同刻重打：未 --force → 1；--force → 0 且仍单包（time 钉死保确定性）
    assert _run(data_root, archives, "--apply") == 1
    assert _run(data_root, archives, "--apply", "--force") == 0
    assert len(list((archives / GUILD / "packages").glob("*.tar.zst"))) == 1

    # 空窗：exit 0、零落包
    assert _run(data_root, archives, "--apply", from_ymd="20200101", to_ymd="20200102") == 0
    assert len(list((archives / GUILD / "packages").glob("*.tar.zst"))) == 1

    # 计数不一致树 → 打包前对账 exit 3（负例的 CLI 面）
    from src.entity_store import comment_path

    comment_path(data_root, GUILD, COMMENT_1).write_bytes(b"{ torn")
    assert _run(data_root, archives, "--apply", "--force") == 3
