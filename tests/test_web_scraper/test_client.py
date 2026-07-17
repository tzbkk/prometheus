"""Tests for src/web_scraper/client.py — QQWebClient.

Run with:
    python -m pytest tests/test_web_scraper/test_client.py -v

All network access is mocked via unittest.mock.patch on the opener's
``open`` method. No real HTTP calls are made.
"""

import json
import os
import sys
import unittest
import urllib.error
from email.message import Message
from typing import Any, cast
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.client import QQWebClient  # noqa: E402

GUILD_ID = "7743321643036658"
GUILD_NUMBER = "Takagi3channel"


def _make_mock_response(payload: dict) -> MagicMock:
    body = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = body
    return cm


def _make_client() -> QQWebClient:
    """Build a QQWebClient without the real session-init GET (opener is mocked)."""
    fake_opener = MagicMock()
    fake_opener.open.return_value = _make_mock_response({"code": 0, "data": {}})
    with patch("src.web_scraper.client.urllib.request.build_opener", return_value=fake_opener):
        client = QQWebClient(GUILD_ID, GUILD_NUMBER)
    return client


def _mock_open(client: QQWebClient) -> Any:
    """Return the opener's mock ``open`` method (opener is a MagicMock in tests)."""
    return cast(MagicMock, client.opener).open


def _make_http_error(code: int, msg: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.com", code=code, msg=msg, hdrs=Message(), fp=None
    )


class TestInitSession(unittest.TestCase):
    def test_init_session_seeds_cookies(self):
        fake_opener = MagicMock()
        fake_opener.open.return_value = _make_mock_response({"code": 0, "data": {}})
        with patch("src.web_scraper.client.urllib.request.build_opener", return_value=fake_opener):
            QQWebClient(GUILD_ID, GUILD_NUMBER)
        self.assertEqual(fake_opener.open.call_count, 1)
        req = fake_opener.open.call_args[0][0]
        self.assertEqual(req.full_url, f"https://pd.qq.com/g/{GUILD_ID}")


class TestGetFeeds(unittest.TestCase):
    def test_get_feeds_returns_correct_tuple(self):
        client = _make_client()
        payload = {
            "code": 0,
            "msg": "ok",
            "data": {
                "vecFeed": [1, 2, 3],
                "feedAttchInfo": "cursor123",
                "isFinish": False,
            },
        }
        with patch.object(client, "_post", return_value=payload) as mock_post:
            vec_feed, attch, finished = client.get_feeds()
        self.assertEqual(vec_feed, [1, 2, 3])
        self.assertEqual(attch, "cursor123")
        self.assertFalse(finished)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "GetGuildFeeds")
        self.assertEqual(kwargs.get("service_type"), 12)

    def test_get_feeds_empty_response(self):
        client = _make_client()
        payload = {"code": 0, "data": {}}
        with patch.object(client, "_post", return_value=payload):
            vec_feed, attch, finished = client.get_feeds()
        self.assertEqual(vec_feed, [])
        self.assertEqual(attch, "")
        self.assertFalse(finished)


class TestGetFeedComments(unittest.TestCase):
    def test_get_feed_comments_returns_correct_tuple(self):
        client = _make_client()
        payload = {
            "code": 0,
            "data": {
                "vecComment": [{"id": "c1"}, {"id": "c2"}],
                "totalNum": 42,
                "attchInfo": "next-cursor",
            },
        }
        with patch.object(client, "_post", return_value=payload) as mock_post:
            vec_comment, total, attch = client.get_feed_comments("B_feed123")
        self.assertEqual(vec_comment, [{"id": "c1"}, {"id": "c2"}])
        self.assertEqual(total, 42)
        self.assertEqual(attch, "next-cursor")
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "GetFeedComments")
        self.assertEqual(kwargs.get("service_type"), 5)


class TestPostHeaders(unittest.TestCase):
    def test_post_sets_correct_headers_feeds(self):
        client = _make_client()
        open_mock = _mock_open(client)
        open_mock.reset_mock()
        open_mock.return_value = _make_mock_response({"code": 0, "data": {}})
        client.get_feeds()
        req = open_mock.call_args[0][0]
        headers = req.headers  # urllib stores keys title-cased
        self.assertEqual(headers.get("X-qq-client-appid"), "537246381")
        self.assertEqual(headers.get("Content-type"), "application/json")
        self.assertEqual(headers.get("Referer"), f"https://pd.qq.com/g/{GUILD_ID}")
        self.assertEqual(json.loads(headers["X-oidb"]), {"uint32_service_type": 12})

    def test_post_sets_correct_headers_comments(self):
        client = _make_client()
        open_mock = _mock_open(client)
        open_mock.reset_mock()
        open_mock.return_value = _make_mock_response({"code": 0, "data": {}})
        client.get_feed_comments("B_feed123")
        req = open_mock.call_args[0][0]
        headers = req.headers
        self.assertEqual(json.loads(headers["X-oidb"]), {"uint32_service_type": 5})

    def test_post_body_contains_guild_id(self):
        client = _make_client()
        open_mock = _mock_open(client)
        open_mock.return_value = _make_mock_response({"code": 0, "data": {}})
        client.get_feeds(from_=7, feed_attch_info="abc")
        req = open_mock.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["guild_id"], GUILD_ID)
        self.assertEqual(body["from"], 7)
        self.assertEqual(body["feedAttchInfo"], "abc")

    def test_post_body_contains_channel_sign(self):
        client = _make_client()
        open_mock = _mock_open(client)
        open_mock.return_value = _make_mock_response({"code": 0, "data": {}})
        client.get_feed_comments("B_xyz", list_num=20, attch_info="cur")
        req = open_mock.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["feedId"], "B_xyz")
        self.assertEqual(body["listNum"], 20)
        self.assertEqual(body["attchInfo"], "cur")
        self.assertEqual(body["channelSign"], {"guild_number": GUILD_NUMBER})
        self.assertEqual(
            body["extInfo"],
            {
                "mapInfo": [
                    {"key": "qc-tabid", "value": ""},
                    {"key": "qc-pageid", "value": ""},
                ]
            },
        )


class TestRetry(unittest.TestCase):
    def test_retry_on_server_error(self):
        """Two HTTPError responses, then success on the third attempt."""
        client = _make_client()
        open_mock = _mock_open(client)
        open_mock.reset_mock()
        good = _make_mock_response({"code": 0, "data": {"vecFeed": ["ok"]}})
        err = _make_http_error(500, "Server Error")
        open_mock.side_effect = [err, err, good]
        with patch("src.web_scraper.client.time.sleep"):  # skip real delays
            vec_feed, _, _ = client.get_feeds()
        self.assertEqual(vec_feed, ["ok"])
        self.assertEqual(open_mock.call_count, 3)

    def test_retry_exhausted_raises(self):
        """All three attempts fail → raise the last HTTPError."""
        client = _make_client()
        open_mock = _mock_open(client)
        open_mock.reset_mock()
        err = _make_http_error(503, "Service Unavailable")
        open_mock.side_effect = err
        with patch("src.web_scraper.client.time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                client.get_feeds()
        self.assertEqual(open_mock.call_count, 3)


if __name__ == "__main__":
    unittest.main()
