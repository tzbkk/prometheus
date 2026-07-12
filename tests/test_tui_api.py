import unittest
from unittest.mock import MagicMock

import sys
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.tui.api import PrometheusApiClient, LauncherApiClient
from src.tui.api_client import ApiClient


class TestPrometheusApiClient(unittest.TestCase):
    """Test PrometheusApiClient wrapper"""

    def setUp(self):
        self.mock_client = MagicMock(spec=ApiClient)
        self.api = PrometheusApiClient(self.mock_client)

    def test_is_healthy_returns_true_on_success(self):
        """is_healthy() returns True when health() succeeds"""
        self.mock_client.health.return_value = True
        result = self.api.is_healthy()
        self.assertTrue(result)
        self.mock_client.health.assert_called_once()

    def test_is_healthy_returns_false_on_exception(self):
        """is_healthy() returns False when health() raises Exception"""
        self.mock_client.health.side_effect = ConnectionError("refused")
        result = self.api.is_healthy()
        self.assertFalse(result)
        self.mock_client.health.assert_called_once()

    def test_is_healthy_returns_false_on_any_exception(self):
        """is_healthy() returns False on any exception type"""
        self.mock_client.health.side_effect = Exception("boom")
        self.assertFalse(self.api.is_healthy())

    def test_get_dashboard_data_aggregates_all(self):
        """get_dashboard_data() aggregates stats+logs+config"""
        self.mock_client.get_stats.return_value = {"feeds": 10}
        self.mock_client.get_logs.return_value = {"logs": [], "total": 0}
        self.mock_client.get_config.return_value = {"api_port": 9420}

        result = self.api.get_dashboard_data()

        self.assertEqual(result["stats"], {"feeds": 10})
        self.assertEqual(result["logs"], {"logs": [], "total": 0})
        self.assertEqual(result["config"], {"api_port": 9420})
        self.assertNotIn("error", result)

    def test_get_dashboard_data_handles_partial_failure(self):
        """get_dashboard_data() handles partial failure (get_stats fails)"""
        self.mock_client.get_stats.side_effect = ConnectionError("no stats")
        self.mock_client.get_logs.return_value = {"logs": [], "total": 0}
        self.mock_client.get_config.return_value = {"api_port": 9420}

        result = self.api.get_dashboard_data()

        self.assertNotIn("stats", result)
        self.assertEqual(result["logs"], {"logs": [], "total": 0})
        self.assertEqual(result["config"], {"api_port": 9420})
        self.assertIn("error", result)

    def test_get_dashboard_data_handles_all_failure(self):
        """get_dashboard_data() returns error dict when all calls fail"""
        self.mock_client.get_stats.side_effect = ConnectionError("x")
        self.mock_client.get_logs.side_effect = ConnectionError("x")
        self.mock_client.get_config.side_effect = ConnectionError("x")

        result = self.api.get_dashboard_data()

        self.assertIn("error", result)

    def test_update_config_calls_set_config(self):
        """update_config() delegates to client.set_config"""
        self.mock_client.set_config.return_value = {"applied": True}
        new_config = {"log_level": "DEBUG"}
        result = self.api.update_config(new_config)

        self.assertEqual(result, {"applied": True})
        self.mock_client.set_config.assert_called_once_with(new_config)

    def test_trigger_daemon_calls_trigger_daemon(self):
        """trigger_daemon() delegates to client.trigger_daemon"""
        self.mock_client.trigger_daemon.return_value = {"triggered": True}
        result = self.api.trigger_daemon()

        self.assertEqual(result, {"triggered": True})
        self.mock_client.trigger_daemon.assert_called_once()


class TestLauncherApiClient(unittest.TestCase):
    """Test LauncherApiClient wrapper"""

    def setUp(self):
        self.mock_client = MagicMock(spec=ApiClient)
        self.api = LauncherApiClient(self.mock_client)

    def test_get_status_calls_launcher_status(self):
        """get_status() delegates to client.launcher_status"""
        self.mock_client.launcher_status.return_value = {"qq": "running"}
        result = self.api.get_status()

        self.assertEqual(result, {"qq": "running"})
        self.mock_client.launcher_status.assert_called_once()

    def test_start_qq_calls_launcher_start(self):
        """start_qq() delegates to client.launcher_start"""
        self.mock_client.launcher_start.return_value = {"qq": "started"}
        result = self.api.start_qq()

        self.assertEqual(result, {"qq": "started"})
        self.mock_client.launcher_start.assert_called_once()

    def test_stop_qq_calls_launcher_stop(self):
        """stop_qq() delegates to client.launcher_stop"""
        self.mock_client.launcher_stop.return_value = {"qq": "stopped"}
        result = self.api.stop_qq()

        self.assertEqual(result, {"qq": "stopped"})
        self.mock_client.launcher_stop.assert_called_once()

    def test_restart_qq_returns_result_on_success(self):
        """restart_qq() returns result on success"""
        self.mock_client.launcher_restart.return_value = {"qq": "restarted"}
        result = self.api.restart_qq()

        self.assertEqual(result, {"qq": "restarted"})
        self.mock_client.launcher_restart.assert_called_once()

    def test_restart_qq_handles_timeout(self):
        """restart_qq() handles TimeoutError gracefully"""
        self.mock_client.launcher_restart.side_effect = TimeoutError("timed out")
        result = self.api.restart_qq()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "restart timeout")

    def test_shutdown_calls_launcher_shutdown(self):
        """shutdown() delegates to client.launcher_shutdown"""
        self.mock_client.launcher_shutdown.return_value = {}
        result = self.api.shutdown()

        self.assertEqual(result, {})
        self.mock_client.launcher_shutdown.assert_called_once()


if __name__ == '__main__':
    unittest.main()
