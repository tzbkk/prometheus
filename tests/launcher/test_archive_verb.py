"""shell archive 动词的 dispatcher 面。

同步直调 src/archive/engine（批处理——engine 跑多久 shell 就等多久）；
消息面与 scripts/archive.py CLI 逐字对齐；引擎 exit-code 语义转 shell
错误消息（WindowError→2 面 / ReconciliationError→3 面 / 其余→1 面）。
缝：monkeypatch src.archive.engine.plan_package / write_package（dispatcher
延迟导入，调用时取模块现值——缝替身生效）。
"""

from pathlib import Path
from types import SimpleNamespace

from src.archive.engine import WindowError
from src.launcher.commands import Cmd, CommandParser, Dispatcher


def test_archive_verb_dispatches_engine_and_maps_exit_codes_to_messages(
    tmp_path, monkeypatch
):
    dispatcher = Dispatcher(pm=SimpleNamespace(), config={}, config_path=None)
    calls = {}

    fake_plan = SimpleNamespace(
        is_empty=False,
        counts={"feeds": 2, "comments": 1, "replies": 1},
        media=(1, 2, 3),
    )

    def fake_plan_package(data_root, guild, from_ymd, to_ymd, **kwargs):
        calls["plan"] = (Path(data_root).name, guild, from_ymd, to_ymd)
        return fake_plan

    def fake_write_package(plan, output, level=7, force=False):
        calls["write"] = (Path(output).name, force)
        out = tmp_path / "pkg.tar.zst"
        out.write_bytes(b"x" * 128)
        return out

    monkeypatch.setattr("src.archive.engine.plan_package", fake_plan_package)
    monkeypatch.setattr("src.archive.engine.write_package", fake_write_package)

    def _cmd(from_ymd, to_ymd, **overrides):
        opts = {"apply": False, "force": False, "output": None}
        opts.update(overrides)
        return Cmd(verb="archive", noun="1000000000000001",
                   args=[from_ymd, to_ymd, opts])

    # dry-run：默认根 data/、计数行 + DRY RUN、零写包
    res = dispatcher.dispatch(_cmd("20220101", "20260830"))
    assert res["ok"] is True
    assert calls["plan"] == ("data", "1000000000000001", "20220101", "20260830")
    assert "window (20220101, 20260830] guild 1000000000000001:" in res["message"]
    assert "feeds=2 comments=1 replies=1 media=3" in res["message"]
    assert "DRY RUN — no package written. Use --apply to create it." in res["message"]
    assert "write" not in calls

    # apply：write_package 被调（output/force 透传），wrote 行
    res = dispatcher.dispatch(
        _cmd("20220101", "20260830", apply=True, force=True, output=str(tmp_path))
    )
    assert res["ok"] is True
    assert "wrote " in res["message"]
    assert calls["write"] == (tmp_path.name, True)

    # 空窗：不落包消息
    monkeypatch.setattr(
        "src.archive.engine.plan_package",
        lambda *a, **k: SimpleNamespace(
            is_empty=True, counts={"feeds": 0, "comments": 0, "replies": 0}, media=()
        ),
    )
    res = dispatcher.dispatch(_cmd("20240101", "20240102"))
    assert res["ok"] is True
    assert "no data in window (20240101, 20240102] for guild 1000000000000001" \
        " — nothing to archive." in res["message"]

    # 坏窗（exit 2 语义）→ 可读 ERROR 消息（引擎消息原文），非栈崩
    def _boom(*args, **kwargs):
        raise WindowError(
            "inverted window: from '20260801' is later than '20260101'"
            " (window is half-open (from, to])"
        )

    monkeypatch.setattr("src.archive.engine.plan_package", _boom)
    res = dispatcher.dispatch(_cmd("20260801", "20260101"))
    assert res["ok"] is False
    assert res["message"] == (
        "ERROR: inverted window: from '20260801' is later than '20260101'"
        " (window is half-open (from, to])"
    )

    # 未知 guild（exit 1 语义）→ 类型名 + 消息
    def _no_guild(*args, **kwargs):
        raise FileNotFoundError("guild directory not found: data/999")

    monkeypatch.setattr("src.archive.engine.plan_package", _no_guild)
    res = dispatcher.dispatch(_cmd("20220101", "20260830"))
    assert res["ok"] is False
    assert res["message"] == "ERROR: FileNotFoundError: guild directory not found: data/999"


def test_archive_bare_lists_guild_data_spans(tmp_path):
    """MD-158：裸 archive / archive <guild> / 未知 guild。

    回答「有哪些时间可以备份」：每 guild 计数 + createTime 跨度
    （YYYYMMDD 同窗参格式）+ 用法两行；guild-only 过滤单行；
    未知 guild 报 known 列表。
    """
    import json as _json
    import os as _os

    guild = "1000000000000001"
    other = "1000000000000002"
    for gid, name, ts in ((guild, "B_x1", 1672531200),
                          (guild, "B_x2", 1735689600),
                          (other, "B_x3", 1704067200)):
        shard = _os.path.join(str(tmp_path), gid, "feeds", name[-2:])
        _os.makedirs(shard, exist_ok=True)
        with open(_os.path.join(shard, name + ".json"), "w",
                  encoding="utf-8") as fh:
            _json.dump({"id": name, "createTime": str(ts)}, fh)

    d = Dispatcher(pm=SimpleNamespace(), config={}, config_path=None,
                   data_root=str(tmp_path))
    parser = CommandParser()

    res = d.dispatch(parser.parse("archive"))
    assert res["ok"] is True
    assert "1000000000000001  2 feeds · 0 comments · 0 replies ·" in res["message"]
    assert "· span 20230101..20250101" in res["message"]
    assert "1000000000000002  1 feeds" in res["message"]
    assert "· span 20240101..20240101" in res["message"]
    assert "usage: archive <guild> <from> <to> [--apply]" in res["message"]
    assert "window (from, to] on entity createTime" in res["message"]

    res = d.dispatch(parser.parse("archive " + other))
    assert res["ok"] is True
    assert other in res["message"] and guild not in res["message"]

    res = d.dispatch(parser.parse("archive 999"))
    assert res["ok"] is False
    assert "known guilds: 1000000000000001, 1000000000000002" in res["message"]


def test_archive_empty_window_appends_data_span(tmp_path, monkeypatch):
    """MD-159：空窗消息尾随实际数据跨度——一步自纠。"""
    import json as _json
    import os as _os

    guild = "1000000000000001"
    for name, ts in (("B_x1", 1672531200), ("B_x2", 1735689600)):
        shard = _os.path.join(str(tmp_path), guild, "feeds", name[-2:])
        _os.makedirs(shard, exist_ok=True)
        with open(_os.path.join(shard, name + ".json"), "w",
                  encoding="utf-8") as fh:
            _json.dump({"id": name, "createTime": str(ts)}, fh)

    monkeypatch.setattr(
        "src.archive.engine.plan_package",
        lambda *a, **k: SimpleNamespace(
            is_empty=True, counts={"feeds": 0, "comments": 0, "replies": 0},
            media=()),
    )
    d = Dispatcher(pm=SimpleNamespace(), config={}, config_path=None,
                   data_root=str(tmp_path))
    res = d.dispatch(CommandParser().parse(
        "archive {0} 20240101 20240102".format(guild)))
    assert res["ok"] is True
    assert "no data in window (20240101, 20240102] for guild {0}"         " — nothing to archive.  data spans 20230101..20250101".format(
            guild) in res["message"]
