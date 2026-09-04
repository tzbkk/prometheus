"""Shell 核心命令解析烟雾（·三目标 scraper/deepbackfill/viewer）——纯 CommandParser 面，零进程零 I/O。"""

import pytest

from src.launcher.commands import (
    CommandParser,
    InvalidTargetError,
    MissingArgumentError,
    UnknownCommandError,
)

TARGETS = ("scraper", "deepbackfill", "viewer")


def test_shell_parses_start_stop_restart_and_logs_for_every_target():
    parser = CommandParser()
    for verb in ("start", "stop", "restart", "logs"):
        for target in TARGETS:
            cmd = parser.parse(f"{verb} {target}")
            assert cmd.verb == verb
            assert cmd.noun == target
            assert cmd.args == []


def test_shell_parses_core_commands_and_stats():
    parser = CommandParser()
    with pytest.raises(UnknownCommandError, match="Unknown command: 'status'"):
        parser.parse("status")
    assert parser.parse("quit").verb == "quit"
    assert parser.parse("health").verb == "health"
    assert parser.parse("help").verb == "help"
    assert parser.parse("clear").verb == "clear"
    assert parser.parse("").verb == "noop"

    cmd = parser.parse("  CONFIG   SHOW ")
    assert (cmd.verb, cmd.noun) == ("config", "show")

    cmd = parser.parse("config set scraper_max_workers 25")
    assert (cmd.verb, cmd.noun) == ("config", "set")
    assert cmd.args == ["scraper_max_workers", "25"]

    assert parser.parse("stats").verb == "stats"


def test_shell_rejects_unknown_verb_missing_target_and_invalid_target():
    parser = CommandParser()
    with pytest.raises(UnknownCommandError):
        parser.parse("frobnicate scraper")
    with pytest.raises(MissingArgumentError):
        parser.parse("start")
    with pytest.raises(InvalidTargetError):
        parser.parse("start qq")
    with pytest.raises(MissingArgumentError):
        parser.parse("config")
    with pytest.raises(MissingArgumentError):
        parser.parse("config set some_key")
    with pytest.raises(UnknownCommandError):
        parser.parse("tail nonsense")
    with pytest.raises(MissingArgumentError):
        parser.parse("stats extra")


def test_shell_parses_archive_verb_window_and_flags():
    parser = CommandParser()

    cmd = parser.parse("archive 7743321643036658 20220101 20260830")
    assert cmd.verb == "archive"
    assert cmd.noun == "7743321643036658"
    assert cmd.args == [
        "20220101", "20260830", {"apply": False, "force": False, "output": None},
    ]

    cmd = parser.parse("archive 1000000000000001 20230601 20230602 --apply --force")
    assert cmd.args[2] == {"apply": True, "force": True, "output": None}

    cmd = parser.parse("archive 1000000000000001 20230601 20230602 --output /tmp/ar")
    assert cmd.args[2] == {"apply": False, "force": False, "output": "/tmp/ar"}

    # 参数面负例：参数不足 / 未知旗标 / --output 缺值
    with pytest.raises(MissingArgumentError):
        parser.parse("archive 1000000000000001 20230601")
    with pytest.raises(InvalidTargetError):
        parser.parse("archive 1000000000000001 20230601 20230602 --bogus")
    with pytest.raises(MissingArgumentError):
        parser.parse("archive 1000000000000001 20230601 20230602 --output")
