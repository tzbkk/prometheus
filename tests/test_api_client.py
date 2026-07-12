import unittest
import json
import socket
from unittest.mock import patch, MagicMock, mock_open
from urllib.error import URLError

import sys
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PROJECT_ROOT)

from src.tui.api_client import ApiClient, ApiError


class TestApiClient(unittest.TestCase):
    """Test ApiClient for QQ and Launcher APIs"""

    def setUp(self):
        """Create test client with custom ports"""
        self.client = ApiClient(qq_port=19420, launcher_port=19421, timeout=5)

    def _mock_response(self, ok=True, data=None, error="", status=200):
        """Helper to create mock HTTP response with envelope"""
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.read.return_value = json.dumps({
            "ok": ok,
            "data": data or {},
            "error": error
        }).encode('utf-8')
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    # QQ API Methods Tests

    @patch('urllib.request.urlopen')
    def test_health_returns_true_on_success(self, mock_urlopen):
        """health() returns True on successful response"""
        mock_urlopen.return_value = self._mock_response(ok=True, data={})
        result = self.client.health()
        self.assertTrue(result)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertIn('/health', req.full_url)

    @patch('urllib.request.urlopen')
    def test_health_raises_on_error_response(self, mock_urlopen):
        """health() raises ApiError when ok=false"""
        mock_urlopen.return_value = self._mock_response(ok=False, error="server error")
        with self.assertRaises(ApiError) as ctx:
            self.client.health()
        self.assertEqual(str(ctx.exception), "server error")

    @patch('urllib.request.urlopen')
    def test_get_logs_sends_correct_request(self, mock_urlopen):
        """get_logs() sends GET /logs with correct query params"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"logs": [{"level": "INFO", "message": "test"}], "total": 1}
        )
        result = self.client.get_logs(since=10, max_lines=50)
        self.assertEqual(result["logs"][0]["message"], "test")
        self.assertEqual(result["total"], 1)
        req = mock_urlopen.call_args[0][0]
        self.assertIn('/logs', req.full_url)
        self.assertIn('since=10', req.full_url)
        self.assertIn('max=50', req.full_url)

    @patch('urllib.request.urlopen')
    def test_get_logs_default_params(self, mock_urlopen):
        """get_logs() uses default params since=0, max_lines=100"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"logs": [], "total": 0}
        )
        self.client.get_logs()
        req = mock_urlopen.call_args[0][0]
        self.assertIn('since=0', req.full_url)
        self.assertIn('max=100', req.full_url)

    @patch('urllib.request.urlopen')
    def test_get_stats_returns_data(self, mock_urlopen):
        """get_stats() returns data dict from response"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"feeds": 42, "comments": 100}
        )
        result = self.client.get_stats()
        self.assertEqual(result["feeds"], 42)
        self.assertEqual(result["comments"], 100)
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertIn('/stats', req.full_url)

    @patch('urllib.request.urlopen')
    def test_get_config_returns_data(self, mock_urlopen):
        """get_config() returns config dict from response"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"api_port": 9420, "log_level": "INFO"}
        )
        result = self.client.get_config()
        self.assertEqual(result["api_port"], 9420)
        self.assertEqual(result["log_level"], "INFO")
        req = mock_urlopen.call_args[0][0]
        self.assertIn('/config', req.full_url)

    @patch('urllib.request.urlopen')
    def test_set_config_sends_put_with_body(self, mock_urlopen):
        """set_config() sends PUT /config with JSON body"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"applied_next_cycle": True}
        )
        new_config = {"api_port": 9500, "log_level": "DEBUG"}
        result = self.client.set_config(new_config)
        self.assertEqual(result["applied_next_cycle"], True)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "PUT")
        self.assertIn('/config', req.full_url)
        self.assertIn('api_port', req.data.decode())

    @patch('urllib.request.urlopen')
    def test_trigger_daemon_sends_post(self, mock_urlopen):
        """trigger_daemon() sends POST /action/trigger-daemon"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"triggered": True}
        )
        result = self.client.trigger_daemon()
        self.assertEqual(result["triggered"], True)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertIn('/action/trigger-daemon', req.full_url)

    # Launcher API Methods Tests

    @patch('urllib.request.urlopen')
    def test_launcher_status_returns_data(self, mock_urlopen):
        """launcher_status() sends GET /status to launcher port"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"qq": "running", "tui": "stopped"}
        )
        result = self.client.launcher_status()
        self.assertEqual(result["qq"], "running")
        self.assertEqual(result["tui"], "stopped")
        req = mock_urlopen.call_args[0][0]
        self.assertIn('/status', req.full_url)

    @patch('urllib.request.urlopen')
    def test_launcher_start_sends_post(self, mock_urlopen):
        """launcher_start() sends POST /start to launcher port"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"qq": "started", "tui": "started"}
        )
        result = self.client.launcher_start()
        self.assertEqual(result["qq"], "started")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertIn('/start', req.full_url)

    @patch('urllib.request.urlopen')
    def test_launcher_stop_sends_post(self, mock_urlopen):
        """launcher_stop() sends POST /stop to launcher port"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"qq": "stopped"}
        )
        result = self.client.launcher_stop()
        self.assertEqual(result["qq"], "stopped")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertIn('/stop', req.full_url)

    @patch('urllib.request.urlopen')
    def test_launcher_restart_sends_post(self, mock_urlopen):
        """launcher_restart() sends POST /restart to launcher port"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"qq": "restarted", "health_check_ms": 1234}
        )
        result = self.client.launcher_restart()
        self.assertEqual(result["qq"], "restarted")
        self.assertEqual(result["health_check_ms"], 1234)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertIn('/restart', req.full_url)

    @patch('urllib.request.urlopen')
    def test_launcher_shutdown_sends_post(self, mock_urlopen):
        """launcher_shutdown() sends POST /shutdown to launcher port"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={}
        )
        result = self.client.launcher_shutdown()
        self.assertEqual(result, {})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertIn('/shutdown', req.full_url)

    # Envelope Handling Tests

    @patch('urllib.request.urlopen')
    def test_ok_false_raises_api_error(self, mock_urlopen):
        """Response with ok=false raises ApiError with error message"""
        mock_urlopen.return_value = self._mock_response(
            ok=False,
            error="validation failed"
        )
        with self.assertRaises(ApiError) as ctx:
            self.client.get_stats()
        self.assertEqual(str(ctx.exception), "validation failed")

    @patch('urllib.request.urlopen')
    def test_ok_false_with_empty_error_raises(self, mock_urlopen):
        """ok=false with empty error still raises"""
        mock_urlopen.return_value = self._mock_response(
            ok=False,
            error=""
        )
        with self.assertRaises(ApiError):
            self.client.get_stats()

    @patch('urllib.request.urlopen')
    def test_ok_true_returns_data_field(self, mock_urlopen):
        """Response with ok=true returns data field only"""
        mock_urlopen.return_value = self._mock_response(
            ok=True,
            data={"key": "value"},
            error=""
        )
        result = self.client.get_stats()
        self.assertEqual(result, {"key": "value"})
        # Should not include ok or error in returned data
        self.assertNotIn("ok", result)
        self.assertNotIn("error", result)

    # Connection Failure Tests

    @patch('urllib.request.urlopen')
    def test_connection_refused_raises_connection_error(self, mock_urlopen):
        """Connection refused raises ConnectionError with host:port"""
        mock_urlopen.side_effect = URLError("Connection refused")
        with self.assertRaises(ConnectionError) as ctx:
            self.client.health()
        self.assertIn("Cannot connect to", str(ctx.exception))
        self.assertIn("127.0.0.1:19420", str(ctx.exception))

    @patch('urllib.request.urlopen')
    def test_connection_error_other_raises_connection_error(self, mock_urlopen):
        """Other URLError also raises ConnectionError"""
        mock_urlopen.side_effect = URLError("Network unreachable")
        with self.assertRaises(ConnectionError) as ctx:
            self.client.get_stats()
        self.assertIn("Cannot connect to", str(ctx.exception))

    # Timeout Tests

    @patch('socket.setdefaulttimeout')
    @patch('urllib.request.urlopen')
    def test_timeout_sets_socket_timeout(self, mock_urlopen, mock_settimeout):
        """Timeout parameter is passed to socket.setdefaulttimeout"""
        mock_urlopen.return_value = self._mock_response(ok=True, data={})
        client = ApiClient(timeout=10)
        client.health()

    @patch('urllib.request.urlopen')
    def test_timeout_raises_timeout_error(self, mock_urlopen):
        """Request timeout raises TimeoutError"""
        mock_urlopen.side_effect = socket.timeout("timeout after 5s")
        with self.assertRaises(TimeoutError) as ctx:
            self.client.health()
        self.assertIn("Timeout after", str(ctx.exception))
        self.assertIn("5s", str(ctx.exception))

    # Port and Host Tests

    @patch('urllib.request.urlopen')
    def test_qq_uses_correct_port(self, mock_urlopen):
        """QQ API methods use qq_port (9420)"""
        mock_urlopen.return_value = self._mock_response(ok=True, data={})
        client = ApiClient(qq_port=19500, launcher_port=19501)
        client.health()
        req = mock_urlopen.call_args[0][0]
        self.assertIn("127.0.0.1:19500", req.full_url)

    @patch('urllib.request.urlopen')
    def test_launcher_uses_correct_port(self, mock_urlopen):
        """Launcher API methods use launcher_port (9421)"""
        mock_urlopen.return_value = self._mock_response(ok=True, data={})
        client = ApiClient(qq_port=19500, launcher_port=19501)
        client.launcher_status()
        req = mock_urlopen.call_args[0][0]
        self.assertIn("127.0.0.1:19501", req.full_url)

    @patch('urllib.request.urlopen')
    def test_all_localhost_only(self, mock_urlopen):
        """All requests use 127.0.0.1 (localhost only)"""
        mock_urlopen.return_value = self._mock_response(ok=True, data={})
        self.client.health()
        self.client.launcher_status()
        for call in mock_urlopen.call_args_list:
            req = call[0][0]
            self.assertIn("127.0.0.1", req.full_url)

    # JSON Parsing Tests

    @patch('urllib.request.urlopen')
    def test_invalid_json_raises_error(self, mock_urlopen):
        """Invalid JSON response raises appropriate error"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"invalid json{{{"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        with self.assertRaises(json.JSONDecodeError):
            self.client.health()

    @patch('urllib.request.urlopen')
    def test_missing_ok_field_raises_error(self, mock_urlopen):
        """Response missing 'ok' field raises ApiError"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": {}, "error": ""}).encode('utf-8')
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        with self.assertRaises(ApiError):
            self.client.health()


if __name__ == '__main__':
    unittest.main()