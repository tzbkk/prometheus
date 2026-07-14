"""Tests for src/launcher/process_manager.py — ProcessManager class.

TDD: written BEFORE implementation. Run with:
    python3 -m pytest tests/test_launcher.py -v
"""

import os
import signal as sigmod
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.launcher.process_manager import ProcessManager  # noqa: E402


class TestProcessManagerInit(unittest.TestCase):
    def test_init_stores_config_and_state(self):
        pm = ProcessManager({"launcher_port": 9421, "max_restarts": 5})
        self.assertEqual(pm.config["launcher_port"], 9421)
        self.assertEqual(pm.processes, {})
        self.assertEqual(pm.restart_counts, {"qq": 0, "tui": 0, "viewer": 0})

    def test_init_project_root_is_parent_of_src(self):
        pm = ProcessManager({})
        self.assertTrue(os.path.isdir(os.path.join(pm.project_root, "src")))
        self.assertTrue(os.path.isfile(os.path.join(pm.project_root, "pyproject.toml")))


class TestStartQq(unittest.TestCase):
    """1. start_qq calls Popen with correct args."""

    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_qq_calls_popen_with_correct_args(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        pm = ProcessManager({"qq_start_script": "scripts/start_qq.sh"})

        pm.start_qq()

        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0], ["bash", "scripts/start_qq.sh"])
        self.assertEqual(kwargs["cwd"], pm.project_root)
        self.assertIs(pm.processes["qq"], mock_proc)

    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_qq_default_script(self, mock_popen):
        pm = ProcessManager({})
        pm.start_qq()
        args, _ = mock_popen.call_args
        self.assertEqual(args[0], ["bash", "scripts/start_qq.sh"])


class TestStartTui(unittest.TestCase):
    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_tui_calls_popen_with_correct_args(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        pm = ProcessManager({"launcher_port": 9421})

        pm.start_tui()

        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(args[0][1:4], ["-m", "src.tui", "--port"])
        self.assertEqual(args[0][4], "9421")
        self.assertEqual(kwargs["cwd"], pm.project_root)
        self.assertIs(pm.processes["tui"], mock_proc)


class TestStartViewer(unittest.TestCase):
    """T14: start_viewer launches python -m src.viewer.backend.server."""

    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_viewer_calls_popen_with_correct_args(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        pm = ProcessManager({"viewer_port": 9422})

        pm.start_viewer()

        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(args[0][1:4], ["-m", "src.viewer.backend.server", "--port"])
        self.assertEqual(args[0][4], "9422")
        self.assertEqual(kwargs["cwd"], pm.project_root)
        self.assertIs(pm.processes["viewer"], mock_proc)

    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_viewer_default_port(self, mock_popen):
        pm = ProcessManager({})
        pm.start_viewer()
        args, _ = mock_popen.call_args
        self.assertEqual(args[0][4], "9422")

    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_viewer_passes_env(self, mock_popen):
        pm = ProcessManager({"viewer_port": 9422})
        pm.start_viewer()
        _, kwargs = mock_popen.call_args
        self.assertIn("env", kwargs)
        self.assertEqual(kwargs["env"], dict(os.environ))

    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_viewer_uses_preexec_pdeathsig(self, mock_popen):
        from src.launcher.process_manager import _set_pdeathsig
        pm = ProcessManager({})
        pm.start_viewer()
        _, kwargs = mock_popen.call_args
        self.assertIs(kwargs["preexec_fn"], _set_pdeathsig)

    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_viewer_no_start_new_session(self, mock_popen):
        """Viewer must NOT use start_new_session — it needs signal delivery."""
        pm = ProcessManager({})
        pm.start_viewer()
        _, kwargs = mock_popen.call_args
        self.assertNotIn("start_new_session", kwargs)

    @patch("src.launcher.process_manager.subprocess.Popen")
    def test_start_viewer_no_stdin_redirect(self, mock_popen):
        """Viewer must NOT redirect stdin — it has no terminal interaction."""
        pm = ProcessManager({})
        pm.start_viewer()
        _, kwargs = mock_popen.call_args
        self.assertNotIn("stdin", kwargs)


class TestStopViewer(unittest.TestCase):
    """T14: stop_viewer calls _stop with viewer + 5s timeout."""

    def test_stop_viewer_sends_sigterm_then_sigkill(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), 0]
        pm.processes["viewer"] = proc

        pm.stop_viewer()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertNotIn("viewer", pm.processes)

    def test_stop_viewer_sends_sigterm_clean_exit(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.wait.return_value = 0
        pm.processes["viewer"] = proc

        pm.stop_viewer()

        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        self.assertNotIn("viewer", pm.processes)

    def test_stop_viewer_uses_5s_timeout(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.wait.return_value = 0
        pm.processes["viewer"] = proc

        pm.stop_viewer()

        _, kwargs = proc.wait.call_args
        self.assertEqual(kwargs.get("timeout"), 5)

    def test_stop_viewer_noop_when_not_running(self):
        pm = ProcessManager({})
        pm.stop_viewer()
        self.assertNotIn("viewer", pm.processes)


class TestStopQq(unittest.TestCase):
    """2. stop_qq sends SIGTERM then SIGKILL if timeout."""

    def test_stop_qq_sends_sigterm_then_exits(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.wait.return_value = 0
        pm.processes["qq"] = proc

        pm.stop_qq()

        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        self.assertNotIn("qq", pm.processes)

    def test_stop_qq_sigkill_after_timeout(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 10), 0]
        pm.processes["qq"] = proc

        pm.stop_qq()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertNotIn("qq", pm.processes)

    def test_stop_qq_noop_when_not_running(self):
        pm = ProcessManager({})
        pm.stop_qq()
        self.assertNotIn("qq", pm.processes)


class TestStopTui(unittest.TestCase):
    def test_stop_tui_sends_sigterm_then_sigkill(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), 0]
        pm.processes["tui"] = proc

        pm.stop_tui()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertNotIn("tui", pm.processes)

    def test_stop_tui_uses_5s_timeout(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.wait.return_value = 0
        pm.processes["tui"] = proc

        pm.stop_tui()

        _, kwargs = proc.wait.call_args
        self.assertEqual(kwargs.get("timeout"), 5)


class TestRestartQq(unittest.TestCase):
    """3. restart_qq calls stop then start then health check."""

    def test_restart_qq_calls_stop_start_healthcheck(self):
        pm = ProcessManager({})
        pm.stop_qq = MagicMock()
        pm.start_qq = MagicMock()
        pm.wait_health_check = MagicMock(return_value=True)

        success, elapsed_ms = pm.restart_qq()

        pm.stop_qq.assert_called_once()
        pm.start_qq.assert_called_once()
        pm.wait_health_check.assert_called_once_with(timeout=30)
        self.assertTrue(success)
        self.assertIsInstance(elapsed_ms, int)
        self.assertGreaterEqual(elapsed_ms, 0)

    def test_restart_qq_returns_false_on_health_failure(self):
        pm = ProcessManager({})
        pm.stop_qq = MagicMock()
        pm.start_qq = MagicMock()
        pm.wait_health_check = MagicMock(return_value=False)

        success, elapsed_ms = pm.restart_qq()
        self.assertFalse(success)
        self.assertIsInstance(elapsed_ms, int)

    def test_restart_qq_ordering(self):
        pm = ProcessManager({})
        order = []
        pm.stop_qq = MagicMock(side_effect=lambda: order.append("stop"))
        pm.start_qq = MagicMock(side_effect=lambda: order.append("start"))
        pm.wait_health_check = MagicMock(
            return_value=True, side_effect=lambda **k: order.append("health") or True
        )
        pm.restart_qq()
        self.assertEqual(order, ["stop", "start", "health"])


class TestCanRestart(unittest.TestCase):
    """4. can_restart returns False when max exceeded."""

    def test_can_restart_true_under_limit(self):
        pm = ProcessManager({"max_restarts": 5})
        pm.restart_counts["qq"] = 3
        self.assertTrue(pm.can_restart("qq"))

    def test_can_restart_false_at_limit(self):
        pm = ProcessManager({"max_restarts": 5})
        pm.restart_counts["qq"] = 5
        self.assertFalse(pm.can_restart("qq"))

    def test_can_restart_false_above_limit(self):
        pm = ProcessManager({"max_restarts": 2})
        pm.restart_counts["qq"] = 10
        self.assertFalse(pm.can_restart("qq"))

    def test_can_restart_default_max_5(self):
        pm = ProcessManager({})
        pm.restart_counts["tui"] = 5
        self.assertFalse(pm.can_restart("tui"))
        pm.restart_counts["tui"] = 4
        self.assertTrue(pm.can_restart("tui"))


class TestAutoRestart(unittest.TestCase):
    """5. auto_restart increments counter."""

    def test_auto_restart_increments_counter_and_returns_true(self):
        pm = ProcessManager({"max_restarts": 5})
        pm.restart_qq = MagicMock()
        before = pm.restart_counts["qq"]
        result = pm.auto_restart("qq")
        self.assertTrue(result)
        self.assertEqual(pm.restart_counts["qq"], before + 1)
        pm.restart_qq.assert_called_once()

    def test_auto_restart_returns_false_when_exceeded(self):
        pm = ProcessManager({"max_restarts": 2})
        pm.restart_qq = MagicMock()
        pm.restart_counts["qq"] = 2
        result = pm.auto_restart("qq")
        self.assertFalse(result)
        self.assertEqual(pm.restart_counts["qq"], 2)
        pm.restart_qq.assert_not_called()

    def test_auto_restart_tui_increments(self):
        pm = ProcessManager({"max_restarts": 5})
        pm.stop_tui = MagicMock()
        pm.start_tui = MagicMock()
        before = pm.restart_counts["tui"]
        result = pm.auto_restart("tui")
        self.assertTrue(result)
        self.assertEqual(pm.restart_counts["tui"], before + 1)

    def test_auto_restart_viewer_increments(self):
        pm = ProcessManager({"max_restarts": 5})
        pm.stop_viewer = MagicMock()
        pm.start_viewer = MagicMock()
        before = pm.restart_counts["viewer"]
        result = pm.auto_restart("viewer")
        self.assertTrue(result)
        self.assertEqual(pm.restart_counts["viewer"], before + 1)
        pm.stop_viewer.assert_called_once()
        pm.start_viewer.assert_called_once()


class TestGracefulShutdown(unittest.TestCase):
    """6. graceful_shutdown calls stop_qq before stop_tui."""

    def test_graceful_shutdown_calls_all_stops(self):
        pm = ProcessManager({})
        pm.stop_qq = MagicMock()
        pm.stop_tui = MagicMock()
        pm.stop_viewer = MagicMock()
        pm.graceful_shutdown()
        pm.stop_qq.assert_called_once()
        pm.stop_tui.assert_called_once()
        pm.stop_viewer.assert_called_once()

    def test_graceful_shutdown_qq_before_tui_before_viewer(self):
        pm = ProcessManager({})
        order = []
        pm.stop_qq = MagicMock(side_effect=lambda: order.append("qq"))
        pm.stop_tui = MagicMock(side_effect=lambda: order.append("tui"))
        pm.stop_viewer = MagicMock(side_effect=lambda: order.append("viewer"))
        pm.graceful_shutdown()
        self.assertEqual(order, ["qq", "tui", "viewer"])


class TestMonitor(unittest.TestCase):
    """7. monitor detects crashed process."""

    def test_monitor_no_crash_when_running(self):
        pm = ProcessManager({})
        qq = MagicMock()
        qq.poll.return_value = None
        tui = MagicMock()
        tui.poll.return_value = None
        pm.processes = {"qq": qq, "tui": tui}
        pm.auto_restart = MagicMock()

        pm.monitor()

        pm.auto_restart.assert_not_called()

    def test_monitor_detects_crashed_qq(self):
        pm = ProcessManager({})
        qq = MagicMock()
        qq.poll.return_value = 1
        tui = MagicMock()
        tui.poll.return_value = None
        pm.processes = {"qq": qq, "tui": tui}
        pm.auto_restart = MagicMock(return_value=True)

        pm.monitor()

        pm.auto_restart.assert_called_once_with("qq")

    def test_monitor_detects_crashed_tui(self):
        pm = ProcessManager({})
        qq = MagicMock()
        qq.poll.return_value = None
        tui = MagicMock()
        tui.poll.return_value = -1
        pm.processes = {"qq": qq, "tui": tui}
        pm.auto_restart = MagicMock(return_value=True)

        pm.monitor()

        pm.auto_restart.assert_called_once_with("tui")

    def test_monitor_detects_crashed_viewer(self):
        pm = ProcessManager({})
        viewer = MagicMock()
        viewer.poll.return_value = 1
        pm.processes = {"viewer": viewer}
        pm.auto_restart = MagicMock(return_value=True)

        pm.monitor()

        pm.auto_restart.assert_called_once_with("viewer")

    def test_monitor_ignores_absent_processes(self):
        pm = ProcessManager({})
        pm.auto_restart = MagicMock()
        pm.monitor()
        pm.auto_restart.assert_not_called()


class TestGetStatus(unittest.TestCase):
    """8. get_status returns correct format."""

    def test_status_all_stopped(self):
        pm = ProcessManager({})
        status = pm.get_status()
        self.assertEqual(status["qq"], "stopped")
        self.assertEqual(status["tui"], "stopped")
        self.assertEqual(status["viewer"], "stopped")
        self.assertEqual(status["restart_counts"], {"qq": 0, "tui": 0, "viewer": 0})

    def test_status_qq_running(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.poll.return_value = None
        pm.processes["qq"] = proc
        self.assertEqual(pm.get_status()["qq"], "running")

    def test_status_qq_crashed(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.poll.return_value = 1
        pm.processes["qq"] = proc
        self.assertEqual(pm.get_status()["qq"], "crashed")

    def test_status_tui_running(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.poll.return_value = None
        pm.processes["tui"] = proc
        self.assertEqual(pm.get_status()["tui"], "running")

    def test_status_tui_crashed_reports_stopped(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.poll.return_value = 2
        pm.processes["tui"] = proc
        self.assertEqual(pm.get_status()["tui"], "stopped")

    def test_status_viewer_running(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.poll.return_value = None
        pm.processes["viewer"] = proc
        self.assertEqual(pm.get_status()["viewer"], "running")

    def test_status_viewer_crashed(self):
        pm = ProcessManager({})
        proc = MagicMock()
        proc.poll.return_value = 1
        pm.processes["viewer"] = proc
        self.assertEqual(pm.get_status()["viewer"], "crashed")

    def test_status_includes_restart_counts(self):
        pm = ProcessManager({})
        pm.restart_counts = {"qq": 3, "tui": 1, "viewer": 2}
        self.assertEqual(pm.get_status()["restart_counts"], {"qq": 3, "tui": 1, "viewer": 2})

    def test_status_keys(self):
        pm = ProcessManager({})
        self.assertEqual(
            set(pm.get_status().keys()),
            {"qq", "tui", "viewer", "restart_counts", "qq_pid"},
        )


class TestWaitHealthCheck(unittest.TestCase):
    @patch("src.launcher.process_manager.urllib.request.urlopen")
    def test_health_check_success(self, mock_urlopen):
        pm = ProcessManager({"api_port": 9420})
        resp = MagicMock()
        resp.__enter__.return_value.status = 200
        resp.__exit__.return_value = False
        mock_urlopen.return_value = resp

        self.assertTrue(pm.wait_health_check(timeout=5))

    @patch("src.launcher.process_manager.urllib.request.urlopen")
    @patch("src.launcher.process_manager.time.sleep")
    @patch("src.launcher.process_manager.time.time")
    def test_health_check_timeout(self, mock_time, mock_sleep, mock_urlopen):
        pm = ProcessManager({"api_port": 9420})
        mock_urlopen.side_effect = Exception("connection refused")
        mock_time.side_effect = [0, 1, 2, 3, 100]

        self.assertFalse(pm.wait_health_check(timeout=30))

    def test_health_check_default_api_port(self):
        pm = ProcessManager({})
        with patch("src.launcher.process_manager.urllib.request.urlopen") as mock_urlopen:
            resp = MagicMock()
            resp.__enter__.return_value.status = 200
            resp.__exit__.return_value = False
            mock_urlopen.return_value = resp
            pm.wait_health_check(timeout=2)
            self.assertIn("9420", mock_urlopen.call_args[0][0])


class TestSignalHandlers(unittest.TestCase):
    @patch("src.launcher.process_manager.signal.signal")
    def test_install_signal_handlers(self, mock_signal):
        pm = ProcessManager({})
        pm.install_signal_handlers()
        registered = [c.args[0] for c in mock_signal.call_args_list]
        self.assertIn(sigmod.SIGTERM, registered)
        self.assertIn(sigmod.SIGINT, registered)


if __name__ == "__main__":
    unittest.main()
