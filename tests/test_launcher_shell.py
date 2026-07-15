"""Tests for src/launcher/commands.py — CommandParser + Dispatcher.

Run with:
    python3 -m pytest tests/test_launcher_shell.py -v
"""

import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.launcher.commands import (  # noqa: E402
    CommandParser,
    Dispatcher,
    Cmd,
    UnknownCommandError,
    MissingArgumentError,
    InvalidTargetError,
    HELP_TEXT,
    START_METHODS,
    STOP_METHODS,
)


def _stopped_status():
    return {
        "qq": "stopped",
        "tui": "stopped",
        "viewer": "stopped",
        "scraper": "stopped",
        "restart_counts": {"qq": 0, "tui": 0, "viewer": 0, "scraper": 0},
        "qq_pid": None,
    }


def _running_status():
    return {
        "qq": "running",
        "tui": "running",
        "viewer": "running",
        "scraper": "stopped",
        "restart_counts": {"qq": 0, "tui": 0, "viewer": 0, "scraper": 0},
        "qq_pid": 12345,
    }


def _make_pm(status=None):
    pm = MagicMock()
    pm.get_status.return_value = status if status is not None else _stopped_status()
    pm.restart_counts = {"qq": 0, "tui": 0, "viewer": 0, "scraper": 0}
    return pm


class TestCommandParser(unittest.TestCase):
    def setUp(self):
        self.p = CommandParser()

    def test_empty_input_returns_noop(self):
        cmd = self.p.parse("")
        self.assertEqual(cmd.verb, "noop")
        self.assertEqual(cmd.noun, "")
        self.assertEqual(cmd.args, [])

    def test_whitespace_only_returns_noop(self):
        for ws in ("   ", "\t", "\n", "  \t\n "):
            cmd = self.p.parse(ws)
            self.assertEqual(cmd.verb, "noop")

    def test_case_insensitive(self):
        lower = self.p.parse("start qq")
        upper = self.p.parse("START QQ")
        mixed = self.p.parse("StArT Qq")
        self.assertEqual(lower, upper)
        self.assertEqual(lower, mixed)
        self.assertEqual(lower, Cmd(verb="start", noun="qq", args=[]))

    def test_all_valid_verbs_without_target(self):
        for verb in ("status", "help", "quit", "clear", "health"):
            cmd = self.p.parse(verb)
            self.assertEqual(cmd.verb, verb)
            self.assertEqual(cmd.noun, "")
            self.assertEqual(cmd.args, [])

    def test_start_stop_restart_with_each_target(self):
        for verb in ("start", "stop", "restart"):
            for target in ("qq", "tui", "viewer"):
                cmd = self.p.parse("{0} {1}".format(verb, target))
                self.assertEqual(cmd.verb, verb)
                self.assertEqual(cmd.noun, target)
                self.assertEqual(cmd.args, [])

    def test_logs_with_each_target(self):
        for target in ("qq", "tui", "viewer"):
            cmd = self.p.parse("logs {0}".format(target))
            self.assertEqual(cmd.verb, "logs")
            self.assertEqual(cmd.noun, target)
            self.assertEqual(cmd.args, [])

    def test_config_show(self):
        cmd = self.p.parse("config show")
        self.assertEqual(cmd.verb, "config")
        self.assertEqual(cmd.noun, "show")
        self.assertEqual(cmd.args, [])

    def test_config_set_key_value(self):
        cmd = self.p.parse("config set port 9423")
        self.assertEqual(cmd.verb, "config")
        self.assertEqual(cmd.noun, "set")
        self.assertEqual(cmd.args, ["port", "9423"])

    def test_config_set_multi_word_value(self):
        cmd = self.p.parse("config set name hello world")
        self.assertEqual(cmd.verb, "config")
        self.assertEqual(cmd.noun, "set")
        self.assertEqual(cmd.args, ["name", "hello world"])

    def test_tail_feeds(self):
        cmd = self.p.parse("tail feeds")
        self.assertEqual(cmd.verb, "tail")
        self.assertEqual(cmd.noun, "feeds")
        self.assertEqual(cmd.args, [])

    def test_tail_stats(self):
        cmd = self.p.parse("tail stats")
        self.assertEqual(cmd.verb, "tail")
        self.assertEqual(cmd.noun, "stats")
        self.assertEqual(cmd.args, [])

    def test_unknown_verb_raises(self):
        with self.assertRaises(UnknownCommandError):
            self.p.parse("xyz")
        with self.assertRaises(UnknownCommandError):
            self.p.parse("frobnicate qq")

    def test_start_without_target_raises(self):
        with self.assertRaises(MissingArgumentError):
            self.p.parse("start")

    def test_stop_without_target_raises(self):
        with self.assertRaises(MissingArgumentError):
            self.p.parse("stop")

    def test_restart_without_target_raises(self):
        with self.assertRaises(MissingArgumentError):
            self.p.parse("restart")

    def test_logs_without_target_raises(self):
        with self.assertRaises(MissingArgumentError):
            self.p.parse("logs")

    def test_invalid_target_raises(self):
        with self.assertRaises(InvalidTargetError):
            self.p.parse("start dummy")
        with self.assertRaises(InvalidTargetError):
            self.p.parse("stop whatever")

    def test_config_without_subcommand_raises(self):
        with self.assertRaises(MissingArgumentError):
            self.p.parse("config")

    def test_config_set_without_key_value_raises(self):
        with self.assertRaises(MissingArgumentError):
            self.p.parse("config set")
        with self.assertRaises(MissingArgumentError):
            self.p.parse("config set onlykey")

    def test_config_invalid_subcommand_raises(self):
        with self.assertRaises(MissingArgumentError):
            self.p.parse("config bogus")

    def test_tail_without_noun_raises(self):
        with self.assertRaises(MissingArgumentError):
            self.p.parse("tail")

    def test_tail_invalid_noun_raises(self):
        with self.assertRaises(InvalidTargetError):
            self.p.parse("tail bogus")

    def test_unknown_command_error_message_format(self):
        try:
            self.p.parse("bogus")
            self.fail("expected UnknownCommandError")
        except UnknownCommandError as e:
            self.assertIn("'bogus'", str(e))
            self.assertIn("help", str(e))

    def test_missing_argument_error_message_includes_verb(self):
        try:
            self.p.parse("start")
            self.fail("expected MissingArgumentError")
        except MissingArgumentError as e:
            self.assertIn("'start'", str(e))

    def test_invalid_target_error_message_includes_noun(self):
        try:
            self.p.parse("start dummy")
            self.fail("expected InvalidTargetError")
        except InvalidTargetError as e:
            self.assertIn("'dummy'", str(e))


class TestDispatcherStart(unittest.TestCase):
    def test_start_qq_when_stopped_calls_start_qq(self):
        pm = _make_pm(_stopped_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="start", noun="qq"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "qq started.")
        pm.start_qq.assert_called_once()

    def test_start_qq_when_running_returns_error(self):
        pm = _make_pm(_running_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="start", noun="qq"))
        self.assertFalse(r["ok"])
        self.assertIn("already running", r["message"])
        pm.start_qq.assert_not_called()

    def test_start_tui_calls_start_tui(self):
        pm = _make_pm(_stopped_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        d.dispatch(Cmd(verb="start", noun="tui"))
        pm.start_tui.assert_called_once()

    def test_start_viewer_calls_start_viewer(self):
        pm = _make_pm(_stopped_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        d.dispatch(Cmd(verb="start", noun="viewer"))
        pm.start_viewer.assert_called_once()

    def test_start_uses_start_methods_map(self):
        self.assertEqual(
            START_METHODS,
            {"qq": "start_qq", "tui": "start_tui",
             "viewer": "start_viewer", "scraper": "start_scraper"},
        )

    def test_start_when_crashed_still_starts(self):
        status = _stopped_status()
        status["qq"] = "crashed"
        pm = _make_pm(status)
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="start", noun="qq"))
        self.assertTrue(r["ok"])
        pm.start_qq.assert_called_once()

    def test_start_resets_restart_counts(self):
        pm = _make_pm(_stopped_status())
        pm.restart_counts["qq"] = 3
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        d.dispatch(Cmd(verb="start", noun="qq"))
        self.assertEqual(pm.restart_counts["qq"], 0)


class TestDispatcherStop(unittest.TestCase):
    def test_stop_qq_when_running_calls_stop_qq(self):
        pm = _make_pm(_running_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="stop", noun="qq"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "qq stopped.")
        pm.stop_qq.assert_called_once()

    def test_stop_qq_when_stopped_returns_error(self):
        pm = _make_pm(_stopped_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="stop", noun="qq"))
        self.assertFalse(r["ok"])
        self.assertIn("already stopped", r["message"])
        pm.stop_qq.assert_not_called()

    def test_stop_tui_calls_stop_tui(self):
        pm = _make_pm(_running_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        d.dispatch(Cmd(verb="stop", noun="tui"))
        pm.stop_tui.assert_called_once()

    def test_stop_viewer_calls_stop_viewer(self):
        pm = _make_pm(_running_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        d.dispatch(Cmd(verb="stop", noun="viewer"))
        pm.stop_viewer.assert_called_once()

    def test_stop_uses_stop_methods_map(self):
        self.assertEqual(
            STOP_METHODS,
            {"qq": "stop_qq", "tui": "stop_tui",
             "viewer": "stop_viewer", "scraper": "stop_scraper"},
        )

    def test_stop_when_crashed_still_stops(self):
        status = _running_status()
        status["viewer"] = "crashed"
        pm = _make_pm(status)
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="stop", noun="viewer"))
        self.assertTrue(r["ok"])
        pm.stop_viewer.assert_called_once()


class TestDispatcherRestart(unittest.TestCase):
    def test_restart_qq_calls_restart_qq(self):
        pm = _make_pm(_running_status())
        pm.restart_qq.return_value = (True, 1234)
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="restart", noun="qq"))
        pm.restart_qq.assert_called_once()
        self.assertTrue(r["ok"])
        self.assertIn("health: OK", r["message"])
        self.assertEqual(r["data"], {"health_check_ms": 1234})

    def test_restart_qq_health_timeout(self):
        pm = _make_pm(_running_status())
        pm.restart_qq.return_value = (False, 5000)
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="restart", noun="qq"))
        self.assertFalse(r["ok"])
        self.assertIn("TIMEOUT", r["message"])
        self.assertEqual(r["data"], {"health_check_ms": 5000})

    def test_restart_qq_resets_restart_counts(self):
        pm = _make_pm(_running_status())
        pm.restart_qq.return_value = (True, 100)
        pm.restart_counts["qq"] = 4
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        d.dispatch(Cmd(verb="restart", noun="qq"))
        self.assertEqual(pm.restart_counts["qq"], 0)

    def test_restart_tui_does_stop_then_start(self):
        pm = _make_pm(_running_status())
        order = []
        pm.stop_tui = MagicMock(side_effect=lambda: order.append("stop"))
        pm.start_tui = MagicMock(side_effect=lambda: order.append("start"))
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="restart", noun="tui"))
        self.assertTrue(r["ok"])
        self.assertEqual(order, ["stop", "start"])

    def test_restart_viewer_does_stop_then_start(self):
        pm = _make_pm(_running_status())
        order = []
        pm.stop_viewer = MagicMock(side_effect=lambda: order.append("stop"))
        pm.start_viewer = MagicMock(side_effect=lambda: order.append("start"))
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="restart", noun="viewer"))
        self.assertTrue(r["ok"])
        self.assertEqual(order, ["stop", "start"])

    def test_restart_tui_resets_restart_counts(self):
        pm = _make_pm(_running_status())
        pm.restart_counts["tui"] = 2
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        d.dispatch(Cmd(verb="restart", noun="tui"))
        self.assertEqual(pm.restart_counts["tui"], 0)


class TestDispatcherStatusAndHealth(unittest.TestCase):
    def test_status_returns_formatted_string(self):
        pm = _make_pm(_running_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="status"))
        self.assertTrue(r["ok"])
        self.assertIn("QQ", r["message"])
        self.assertIn("running", r["message"])
        self.assertIn("restart", r["message"])
        self.assertEqual(r["data"], pm.get_status.return_value)

    def test_status_data_contains_all_fields(self):
        pm = _make_pm(_stopped_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="status"))
        for key in ("qq", "tui", "viewer", "restart_counts", "qq_pid"):
            self.assertIn(key, r["data"])

    def test_health_returns_ok_when_healthy(self):
        pm = _make_pm()
        pm.wait_health_check.return_value = True
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="health"))
        self.assertTrue(r["ok"])
        self.assertIn("OK", r["message"])
        pm.wait_health_check.assert_called_once_with(timeout=5)

    def test_health_returns_timeout_when_unhealthy(self):
        pm = _make_pm()
        pm.wait_health_check.return_value = False
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="health"))
        self.assertFalse(r["ok"])
        self.assertIn("TIMEOUT", r["message"])


class TestDispatcherConfig(unittest.TestCase):
    def test_config_show_returns_json(self):
        config = {"launcher_port": 9421, "api_port": 9420}
        pm = _make_pm()
        d = Dispatcher(pm, config, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="config", noun="show", args=[]))
        self.assertTrue(r["ok"])
        parsed = json.loads(r["message"])
        self.assertEqual(parsed, config)

    def test_config_set_updates_dict(self):
        config = {"launcher_port": 9421}
        pm = _make_pm()
        d = Dispatcher(pm, config, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="config", noun="set", args=["port", "9423"]))
        self.assertTrue(r["ok"])
        self.assertEqual(config["port"], "9423")
        self.assertIn("port", r["message"])

    def test_config_set_multi_word_value(self):
        config = {}
        pm = _make_pm()
        d = Dispatcher(pm, config, "/tmp/conf.json")
        d.dispatch(Cmd(verb="config", noun="set", args=["name", "hello world"]))
        self.assertEqual(config["name"], "hello world")

    def test_config_does_not_touch_pm(self):
        config = {}
        pm = _make_pm()
        d = Dispatcher(pm, config, "/tmp/conf.json")
        d.dispatch(Cmd(verb="config", noun="show"))
        pm.get_status.assert_not_called()


class TestDispatcherSignals(unittest.TestCase):
    def test_help_returns_text(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="help"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], HELP_TEXT)

    def test_quit_returns_quit_action(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="quit"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "Shutting down...")
        self.assertEqual(r["data"], {"action": "quit"})

    def test_clear_returns_clear_action(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="clear"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"], {"action": "clear"})

    def test_noop_returns_empty(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="noop"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "")
        self.assertEqual(r["data"], {})

    def test_logs_returns_tail_log_signal(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="logs", noun="qq"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"], {"action": "tail_log", "target": "qq"})

    def test_tail_feeds_returns_signal(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="tail", noun="feeds"))
        self.assertEqual(r["data"], {"action": "tail_feeds"})

    def test_tail_stats_returns_signal(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="tail", noun="stats"))
        self.assertEqual(r["data"], {"action": "tail_stats"})

    def test_unknown_cmd_raises_value_error(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        with self.assertRaises(ValueError):
            d.dispatch(Cmd(verb="totally-unknown"))


class TestDispatcherThreadSafety(unittest.TestCase):
    def test_thread_lock_serializes_concurrent_calls(self):
        pm = _make_pm(_stopped_status())
        concurrency = {"current": 0, "max": 0}
        guard = threading.Lock()

        def track():
            with guard:
                concurrency["current"] += 1
                if concurrency["current"] > concurrency["max"]:
                    concurrency["max"] = concurrency["current"]
            time.sleep(0.05)
            with guard:
                concurrency["current"] -= 1

        pm.start_qq.side_effect = track
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        cmd = Cmd(verb="start", noun="qq")
        threads = [threading.Thread(target=d.dispatch, args=(cmd,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(concurrency["max"], 1)
        self.assertEqual(pm.start_qq.call_count, 5)

    def test_concurrent_stop_start_use_same_lock(self):
        pm = _make_pm(_running_status())
        seen = {"max": 0, "cur": 0}
        guard = threading.Lock()

        def make_tracker():
            def _t():
                with guard:
                    seen["cur"] += 1
                    if seen["cur"] > seen["max"]:
                        seen["max"] = seen["cur"]
                time.sleep(0.02)
                with guard:
                    seen["cur"] -= 1
            return _t

        pm.stop_qq.side_effect = make_tracker()
        pm.stop_tui.side_effect = make_tracker()
        pm.stop_viewer.side_effect = make_tracker()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        cmds = [
            Cmd(verb="stop", noun="qq"),
            Cmd(verb="stop", noun="tui"),
            Cmd(verb="stop", noun="viewer"),
        ]
        threads = [threading.Thread(target=d.dispatch, args=(c,)) for c in cmds]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(seen["max"], 1)


class TestDispatcherResponseShape(unittest.TestCase):
    def test_every_response_has_three_keys(self):
        pm = _make_pm(_stopped_status())
        pm.restart_qq.return_value = (True, 10)
        pm.wait_health_check.return_value = True
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        parser = CommandParser()
        inputs = [
            "", "help", "quit", "clear", "status", "health",
            "logs qq", "tail feeds", "tail stats", "config show",
            "start qq", "stop qq", "restart qq",
        ]
        for text in inputs:
            r = d.dispatch(parser.parse(text))
            self.assertEqual(set(r.keys()), {"ok", "message", "data"},
                             msg="bad shape for input {0!r}: {1}".format(text, r))
            self.assertIsInstance(r["ok"], bool)
            self.assertIsInstance(r["message"], str)
            self.assertIsInstance(r["data"], dict)


class TestScraperParsing(unittest.TestCase):
    def setUp(self):
        self.p = CommandParser()

    def test_parse_start_scraper(self):
        cmd = self.p.parse("start scraper")
        self.assertEqual(cmd, Cmd(verb="start", noun="scraper", args=[]))

    def test_parse_stop_scraper(self):
        cmd = self.p.parse("stop scraper")
        self.assertEqual(cmd, Cmd(verb="stop", noun="scraper", args=[]))

    def test_parse_restart_scraper(self):
        cmd = self.p.parse("restart scraper")
        self.assertEqual(cmd, Cmd(verb="restart", noun="scraper", args=[]))

    def test_parse_invalid_target_still_works(self):
        with self.assertRaises(InvalidTargetError):
            self.p.parse("start invalid")
        with self.assertRaises(InvalidTargetError):
            self.p.parse("stop frobnicate")


class TestScraperDispatch(unittest.TestCase):
    def test_dispatch_start_scraper(self):
        pm = _make_pm(_stopped_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="start", noun="scraper"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "scraper started.")
        pm.start_scraper.assert_called_once()

    def test_dispatch_stop_scraper(self):
        status = _running_status()
        status["scraper"] = "running"
        status["qq"] = "stopped"
        pm = _make_pm(status)
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="stop", noun="scraper"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "scraper stopped.")
        pm.stop_scraper.assert_called_once()

    def test_scraper_allowed_when_qq_stopped(self):
        status = _stopped_status()
        status["qq"] = "stopped"
        pm = _make_pm(status)
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="start", noun="scraper"))
        self.assertTrue(r["ok"])
        pm.start_scraper.assert_called_once()


class TestPortMutex(unittest.TestCase):
    def test_port_mutex_scraper_blocked_when_qq_running(self):
        status = _running_status()
        self.assertEqual(status["qq"], "running")
        pm = _make_pm(status)
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="start", noun="scraper"))
        self.assertFalse(r["ok"])
        self.assertIn("9420", r["message"])
        self.assertIn("QQ", r["message"])
        pm.start_scraper.assert_not_called()

    def test_port_mutex_qq_blocked_when_scraper_running(self):
        status = _stopped_status()
        status["scraper"] = "running"
        pm = _make_pm(status)
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="start", noun="qq"))
        self.assertFalse(r["ok"])
        self.assertIn("9420", r["message"])
        self.assertIn("scraper", r["message"])
        pm.start_qq.assert_not_called()


class TestScraperStatusAndHelp(unittest.TestCase):
    def test_status_includes_scraper(self):
        pm = _make_pm(_stopped_status())
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="status"))
        self.assertIn("scraper", r["data"])
        self.assertIn("Scraper", r["message"])

    def test_help_text_includes_scraper(self):
        pm = _make_pm()
        d = Dispatcher(pm, {}, "/tmp/conf.json")
        r = d.dispatch(Cmd(verb="help"))
        self.assertIn("scraper", r["message"])


if __name__ == "__main__":
    unittest.main()
