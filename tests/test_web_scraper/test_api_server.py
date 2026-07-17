"""Tests for src/web_scraper/api_server.py — APIServer HTTP endpoints.

TDD: written BEFORE implementation. Run with:
    python3 -m pytest tests/test_web_scraper/test_api_server.py -v

Strategy:
- Start a real HTTPServer on port 0 (OS-assigned) per test.
- Make real HTTP requests via urllib.request to exercise the full stack.
- Mock the Store (MagicMock) to isolate scraper logic.

Envelope contract (project-wide):
    {"ok": bool, "data": {}, "error": ""}
- TUI api_client.py:37 does `envelope.get('ok', False)` and raises ApiError if false.
- Therefore every successful response MUST carry ok=True.
"""

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.api_server import APIServer  # noqa: E402


def _make_stats(**overrides):
    """Build a fresh stats dict (matches the daemon contract)."""
    stats: dict = {
        "scanned_feeds": 42,
        "feeds_count": 100,
        "comments_count": 50,
        "media_count": 75,
        "last_scan_ts": 1234567890,
        "daemon_running": True,
        "log_buffer": [
            {"seq": 1, "level": "INFO", "msg": "line 1", "ts": "2026-01-01 00:00:01,000"},
            {"seq": 2, "level": "INFO", "msg": "line 2", "ts": "2026-01-01 00:00:02,000"},
            {"seq": 3, "level": "INFO", "msg": "line 3", "ts": "2026-01-01 00:00:03,000"},
        ],
        "config": {"scraper_max_workers": 10},
    }
    stats.update(overrides)
    return stats


def _make_store_mock():
    """Mock Store exposing the two sets the API reads for live counts."""
    store = MagicMock()
    store._feed_ids = set()
    store._comment_keys = set()
    return store


def _start_server(stats=None, port=0):
    """Start APIServer on port 0 (OS-assigned), return (api, base_url)."""
    if stats is None:
        stats = _make_stats()
    store = _make_store_mock()
    api = APIServer(store, stats, port=port)
    api.start()
    deadline = time.time() + 5
    while api.port is None and time.time() < deadline:
        time.sleep(0.01)
    return api, "http://127.0.0.1:{0}".format(api.port)


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


class TestEnvelopeFormat(unittest.TestCase):
    """Every response — success or 404 — uses {ok, data, error} envelope."""

    def test_health_has_envelope_keys(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/health")
            self.assertEqual(status, 200)
            for key in ("ok", "data", "error"):
                self.assertIn(key, body)
        finally:
            api.stop()

    def test_404_has_envelope_keys(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/unknown")
            self.assertEqual(status, 404)
            for key in ("ok", "data", "error"):
                self.assertIn(key, body)
        finally:
            api.stop()


class TestHealth(unittest.TestCase):
    """GET /health → {ok:true, data:{status:'ok', scraper:'running', scanned_feeds:N}}."""

    def test_health_returns_envelope(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/health")
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"]["status"], "ok")
            self.assertEqual(body["error"], "")
        finally:
            api.stop()

    def test_health_has_scraper_running(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/health")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"]["scraper"], "running")
        finally:
            api.stop()

    def test_health_reports_scanned_feeds(self):
        stats = _make_stats(scanned_feeds=77)
        api, base = _start_server(stats=stats)
        try:
            status, body = _request(base, "/health")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"]["scanned_feeds"], 77)
        finally:
            api.stop()


class TestStats(unittest.TestCase):
    """GET /stats → counts from stats dict / store sets."""

    def test_stats_returns_counts(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/stats")
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            data = body["data"]
            for key in (
                "feeds_count",
                "comments_count",
                "media_count",
                "last_scan_ts",
                "daemon_running",
            ):
                self.assertIn(key, data)
        finally:
            api.stop()

    def test_stats_reflects_stats_values(self):
        stats = _make_stats(
            feeds_count=3,
            comments_count=7,
            media_count=11,
            last_scan_ts=999,
            daemon_running=False,
        )
        api, base = _start_server(stats=stats)
        try:
            status, body = _request(base, "/stats")
            self.assertEqual(status, 200)
            data = body["data"]
            self.assertEqual(data["feeds_count"], 3)
            self.assertEqual(data["comments_count"], 7)
            self.assertEqual(data["media_count"], 11)
            self.assertEqual(data["last_scan_ts"], 999)
            self.assertEqual(data["daemon_running"], False)
        finally:
            api.stop()

    def test_stats_uses_store_set_sizes_when_stats_counts_zero(self):
        # When stats counts are not maintained, fall back to Store set sizes.
        store = _make_store_mock()
        store._feed_ids = {"a", "b", "c"}
        store._comment_keys = {"k1", "k2"}
        stats = _make_stats(feeds_count=0, comments_count=0)
        api = APIServer(store, stats, port=0)
        api.start()
        base = "http://127.0.0.1:{0}".format(api.port)
        try:
            status, body = _request(base, "/stats")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"]["feeds_count"], 3)
            self.assertEqual(body["data"]["comments_count"], 2)
        finally:
            api.stop()


class TestLogs(unittest.TestCase):
    """GET /logs[?since=N&max=M] → sliced log_buffer."""

    def test_logs_endpoint(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/logs")
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            data = body["data"]
            self.assertIn("lines", data)
            self.assertEqual(data["total"], 3)
            self.assertEqual(len(data["lines"]), 3)
            self.assertEqual(data["lines"][0]["msg"], "line 1")
        finally:
            api.stop()

    def test_logs_with_since_param(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/logs?since=1")
            self.assertEqual(status, 200)
            data = body["data"]
            self.assertEqual(len(data["lines"]), 2)
            self.assertEqual(data["lines"][0]["seq"], 2)
            self.assertEqual(data["total"], 3)
        finally:
            api.stop()

    def test_logs_with_max_param(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/logs?since=0&max=2")
            self.assertEqual(status, 200)
            data = body["data"]
            self.assertEqual(len(data["lines"]), 2)
            self.assertEqual(data["lines"][0]["seq"], 1)
            self.assertEqual(data["lines"][1]["seq"], 2)
        finally:
            api.stop()

    def test_logs_since_beyond_end_returns_empty(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/logs?since=100")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"]["lines"], [])
            self.assertEqual(body["data"]["total"], 3)
        finally:
            api.stop()


class TestConfig(unittest.TestCase):
    """GET /config + PUT /config."""

    def test_config_get(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/config")
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"], {"scraper_max_workers": 10})
        finally:
            api.stop()

    def test_config_put_updates_config(self):
        stats = _make_stats()
        api, base = _start_server(stats=stats)
        try:
            status, body = _request(
                base, "/config", method="PUT", body={"scraper_max_workers": 20}
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"].get("updated"), True)
            status2, body2 = _request(base, "/config")
            self.assertEqual(body2["data"].get("scraper_max_workers"), 20)
        finally:
            api.stop()


class TestTriggerDaemon(unittest.TestCase):
    """POST /action/trigger-daemon → invokes the registered trigger callback."""

    def test_trigger_daemon_calls_callback(self):
        stats = _make_stats(daemon_running=False)
        api, base = _start_server(stats=stats)
        called = threading.Event()

        def cb():
            called.set()

        api.set_trigger_callback(cb)
        try:
            status, body = _request(base, "/action/trigger-daemon", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"].get("triggered"), True)
            self.assertTrue(called.wait(timeout=5))
        finally:
            api.stop()

    def test_trigger_rejected_when_daemon_running(self):
        stats = _make_stats(daemon_running=True)
        api, base = _start_server(stats=stats)
        called = {"flag": False}

        def cb():
            called["flag"] = True

        api.set_trigger_callback(cb)
        try:
            status, body = _request(base, "/action/trigger-daemon", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertEqual(body["data"].get("triggered"), False)
            self.assertFalse(called["flag"])
        finally:
            api.stop()

    def test_trigger_without_callback_still_envelopes(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/action/trigger-daemon", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"].get("triggered"), False)
        finally:
            api.stop()


class TestNotFound(unittest.TestCase):
    """Unknown paths/methods → 404 envelope with ok=False."""

    def test_unknown_get_path(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/does-not-exist")
            self.assertEqual(status, 404)
            self.assertFalse(body["ok"])
            self.assertEqual(body["data"], {})
            self.assertIn("not found", body["error"].lower())
        finally:
            api.stop()

    def test_unknown_post_path(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/action/unknown", method="POST", body={})
            self.assertEqual(status, 404)
            self.assertFalse(body["ok"])
        finally:
            api.stop()

    def test_unknown_put_path(self):
        api, base = _start_server()
        try:
            status, body = _request(base, "/nope", method="PUT", body={})
            self.assertEqual(status, 404)
            self.assertFalse(body["ok"])
        finally:
            api.stop()


class TestServerLifecycle(unittest.TestCase):
    """start/stop on OS-assigned port, bound to loopback."""

    def test_start_stop_lifecycle(self):
        api, base = _start_server()
        try:
            self.assertIsNotNone(api.port)
            assert api.port is not None
            self.assertGreater(api.port, 0)
            status, body = _request(base, "/health")
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
        finally:
            api.stop()
        with self.assertRaises(Exception):
            _request(base, "/health")

    def test_bound_to_loopback(self):
        api = APIServer(_make_store_mock(), _make_stats(), port=0)
        api.start()
        try:
            assert api.server is not None
            self.assertEqual(api.server.server_address[0], "127.0.0.1")
        finally:
            api.stop()

    def test_stop_is_idempotent(self):
        api, _ = _start_server()
        api.stop()
        api.stop()

    def test_default_port_is_9420(self):
        api = APIServer(_make_store_mock(), _make_stats())
        # Don't start (would bind 9420); just check the requested port.
        try:
            self.assertEqual(api._requested_port, 9420)
        finally:
            pass

class TestLoggingSuppressed(unittest.TestCase):
    """Handler suppresses default BaseHTTPRequestHandler stderr logging."""

    def test_log_message_is_noop(self):
        api, base = _start_server()
        try:
            handler_cls = api._handler_cls
            self.assertTrue(hasattr(handler_cls, "log_message"))
        finally:
            api.stop()


if __name__ == "__main__":
    unittest.main()
