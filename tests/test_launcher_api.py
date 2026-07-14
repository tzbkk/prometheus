"""Tests for src/launcher/api.py — LauncherApi HTTP endpoints.

TDD: written BEFORE implementation. Run with:
    python3 -m pytest tests/test_launcher_api.py -v

Strategy:
- Start a real HTTPServer on port 0 (OS-assigned) per test.
- Make real HTTP requests via urllib.request to exercise full stack.
- Mock ProcessManager (MagicMock) to isolate launcher logic.
"""

import json
import os
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.launcher.api import LauncherApi  # noqa: E402


def _make_pm():
    """Create a fresh mock ProcessManager with default get_status."""
    pm = MagicMock()
    pm.get_status.return_value = {
        "qq": "stopped",
        "tui": "stopped",
        "viewer": "stopped",
        "restart_counts": {"qq": 0, "tui": 0, "viewer": 0},
    }
    return pm


def _start_server(pm=None):
    """Start LauncherApi on port 0, return (api, base_url)."""
    if pm is None:
        pm = _make_pm()
    api = LauncherApi(pm, port=0)
    api.start()
    deadline = time.time() + 5
    while api.port is None and time.time() < deadline:
        time.sleep(0.01)
    base = "http://127.0.0.1:{0}".format(api.port)
    return api, base, pm


def _request(base, path, method="GET", body=None):
    """Make an HTTP request, return (status_code, parsed_json)."""
    url = base + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class TestServerLifecycle(unittest.TestCase):
    """Api can start/stop on OS-assigned port, bound to 127.0.0.1."""

    def test_start_assigns_port(self):
        api, base, _ = _start_server()
        try:
            self.assertIsNotNone(api.port)
            assert api.port is not None
            self.assertGreater(api.port, 0)
        finally:
            api.stop()

    def test_bound_to_loopback(self):
        api = LauncherApi(_make_pm(), port=0)
        api.start()
        try:
            assert api.server is not None
            self.assertEqual(api.server.server_address[0], "127.0.0.1")
        finally:
            api.stop()

    def test_stop_is_idempotent(self):
        api, _, _ = _start_server()
        api.stop()
        api.stop()


class TestEnvelopeFormat(unittest.TestCase):
    """8. All responses have unified envelope format {ok, data, error}."""

    def test_envelope_keys_present_on_success(self):
        api, base, _ = _start_server()
        try:
            status, body = _request(base, "/status")
            self.assertEqual(status, 200)
            for key in ("ok", "data", "error"):
                self.assertIn(key, body)
        finally:
            api.stop()

    def test_envelope_keys_present_on_404(self):
        api, base, _ = _start_server()
        try:
            status, body = _request(base, "/nonexistent")
            self.assertEqual(status, 404)
            for key in ("ok", "data", "error"):
                self.assertIn(key, body)
        finally:
            api.stop()


class TestPostStart(unittest.TestCase):
    """1. POST /start calls pm.start_qq + pm.start_tui."""

    def test_start_calls_qq_and_tui(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/start", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"], {"qq": "started", "tui": "started"})
            self.assertEqual(body["error"], "")
            pm.start_qq.assert_called_once_with()
            pm.start_tui.assert_called_once_with()
        finally:
            api.stop()

    def test_start_idempotent_if_already_running(self):
        pm = _make_pm()
        pm.get_status.return_value = {
            "qq": "running",
            "tui": "running",
            "restart_counts": {"qq": 0, "tui": 0},
        }
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/start", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"]["qq"], "running")
            self.assertEqual(body["data"]["tui"], "running")
            pm.start_qq.assert_not_called()
            pm.start_tui.assert_not_called()
        finally:
            api.stop()


class TestPostStop(unittest.TestCase):
    """2. POST /stop calls pm.stop_qq (NOT stop_tui)."""

    def test_stop_calls_only_stop_qq(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/stop", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"], {"qq": "stopped"})
            pm.stop_qq.assert_called_once_with()
            pm.stop_tui.assert_not_called()
        finally:
            api.stop()


class TestPostRestart(unittest.TestCase):
    """3. POST /restart calls pm.restart_qq, returns health_check_ms."""

    def test_restart_success_returns_health_check_ms(self):
        pm = _make_pm()
        pm.restart_qq.return_value = (True, 1234)
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/restart", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"]["qq"], "restarted")
            self.assertEqual(body["data"]["health_check_ms"], 1234)
            self.assertEqual(body["error"], "")
            pm.restart_qq.assert_called_once_with()
        finally:
            api.stop()

    def test_restart_4_timeout_returns_ok_false_with_error(self):
        pm = _make_pm()
        pm.restart_qq.return_value = (False, 30000)
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/restart", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertFalse(body["ok"])
            self.assertEqual(body["data"], {})
            self.assertIn("timeout", body["error"].lower())
            self.assertIn("30", body["error"])
        finally:
            api.stop()


class TestGetStatus(unittest.TestCase):
    """5. GET /status returns correct format."""

    def test_status_returns_pm_status(self):
        pm = _make_pm()
        pm.get_status.return_value = {
            "qq": "running",
            "tui": "running",
            "restart_counts": {"qq": 2, "tui": 1},
        }
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/status")
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"]["qq"], "running")
            self.assertEqual(body["data"]["tui"], "running")
            self.assertEqual(body["data"]["restart_counts"], {"qq": 2, "tui": 1})
            self.assertEqual(body["error"], "")
            pm.get_status.assert_called_once_with()
        finally:
            api.stop()

    def test_status_when_crashed(self):
        pm = _make_pm()
        pm.get_status.return_value = {
            "qq": "crashed",
            "tui": "stopped",
            "restart_counts": {"qq": 3, "tui": 0},
        }
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/status")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"]["qq"], "crashed")
            self.assertEqual(body["data"]["tui"], "stopped")
        finally:
            api.stop()


class TestPostShutdown(unittest.TestCase):
    """6. POST /shutdown calls pm.graceful_shutdown."""

    def test_shutdown_calls_graceful_shutdown(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/shutdown", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"], {})
            self.assertEqual(body["error"], "")
            pm.graceful_shutdown.assert_called_once_with()
        finally:
            api.stop()

    def test_shutdown_response_before_server_stops(self):
        """Server must respond THEN stop. Client gets a valid response."""
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/shutdown", method="POST", body={})
            self.assertEqual(status, 200)
        finally:
            try:
                api.stop()
            except Exception:
                pass


class TestNotFound(unittest.TestCase):
    """7. Unknown path → 404 envelope."""

    def test_unknown_get_path(self):
        api, base, _ = _start_server()
        try:
            status, body = _request(base, "/unknown")
            self.assertEqual(status, 404)
            self.assertFalse(body["ok"])
            self.assertEqual(body["data"], {})
            self.assertIn("not found", body["error"].lower())
        finally:
            api.stop()

    def test_unknown_post_path(self):
        api, base, _ = _start_server()
        try:
            status, body = _request(base, "/explode", method="POST", body={})
            self.assertEqual(status, 404)
            self.assertFalse(body["ok"])
        finally:
            api.stop()

    def test_wrong_method_on_known_path(self):
        """DELETE /status → 404 (no handler for DELETE)."""
        api, base, _ = _start_server()
        try:
            status, body = _request(base, "/status", method="DELETE")
            self.assertEqual(status, 404)
        finally:
            api.stop()


class TestRequestBodyParsing(unittest.TestCase):
    """POST endpoints accept JSON body (and tolerate empty body)."""

    def test_start_accepts_empty_body(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/start", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
        finally:
            api.stop()

    def test_post_with_no_body_data(self):
        """POST with no Content-Length still works."""
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/status", method="POST")
            self.assertEqual(status, 404)
        finally:
            api.stop()


class TestLoggingSuppressed(unittest.TestCase):
    """Handler should suppress default BaseHTTPRequestHandler logging."""

    def test_log_message_is_noop(self):
        api, base, _ = _start_server()
        try:
            handler_cls = api._handler_cls
            self.assertTrue(hasattr(handler_cls, "log_message"))
        finally:
            api.stop()


class TestPostWebappStart(unittest.TestCase):
    """POST /webapp/start calls pm.start_viewer."""

    def test_start_calls_start_viewer(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/webapp/start", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"], {"viewer": "started"})
            self.assertEqual(body["error"], "")
            pm.start_viewer.assert_called_once_with()
        finally:
            api.stop()

    def test_start_idempotent_if_already_running(self):
        pm = _make_pm()
        pm.get_status.return_value = {
            "qq": "stopped",
            "tui": "stopped",
            "viewer": "running",
            "restart_counts": {"qq": 0, "tui": 0, "viewer": 0},
        }
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/webapp/start", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"], {"viewer": "already running"})
            pm.start_viewer.assert_not_called()
        finally:
            api.stop()

    def test_start_accepts_empty_body(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/webapp/start", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
        finally:
            api.stop()


class TestPostWebappStop(unittest.TestCase):
    """POST /webapp/stop calls pm.stop_viewer."""

    def test_stop_calls_stop_viewer(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/webapp/stop", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"], {"viewer": "stopped"})
            self.assertEqual(body["error"], "")
            pm.stop_viewer.assert_called_once_with()
        finally:
            api.stop()

    def test_stop_does_not_touch_qq_or_tui(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/webapp/stop", method="POST", body={})
            self.assertEqual(status, 200)
            pm.stop_qq.assert_not_called()
            pm.stop_tui.assert_not_called()
        finally:
            api.stop()


class TestGetWebappStatus(unittest.TestCase):
    """GET /webapp/status returns viewer status from pm.get_status."""

    def test_status_returns_viewer_running(self):
        pm = _make_pm()
        pm.get_status.return_value = {
            "qq": "running",
            "tui": "running",
            "viewer": "running",
            "restart_counts": {"qq": 0, "tui": 0, "viewer": 1},
        }
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/webapp/status")
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"], {"viewer": "running"})
            self.assertEqual(body["error"], "")
            pm.get_status.assert_called_once_with()
        finally:
            api.stop()

    def test_status_returns_viewer_stopped(self):
        pm = _make_pm()
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/webapp/status")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"], {"viewer": "stopped"})
        finally:
            api.stop()

    def test_status_returns_viewer_crashed(self):
        pm = _make_pm()
        pm.get_status.return_value = {
            "qq": "running",
            "tui": "stopped",
            "viewer": "crashed",
            "restart_counts": {"qq": 0, "tui": 0, "viewer": 2},
        }
        api, base, pm = _start_server(pm)
        try:
            status, body = _request(base, "/webapp/status")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"], {"viewer": "crashed"})
        finally:
            api.stop()


if __name__ == "__main__":
    unittest.main()
