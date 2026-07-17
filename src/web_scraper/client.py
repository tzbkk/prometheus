"""HTTP client for pd.qq.com public web APIs.

Pure stdlib (urllib). No requests/httpx. No browser dependencies.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

logger = logging.getLogger(__name__)

_BASE = "https://pd.qq.com/qunng/guild/gotrpc/noauth/trpc.qchannel.commreader.ComReader/"
_CLIENT_APPID = "537246381"
_RETRY_DELAYS = (1.0, 2.0, 4.0)
_MAX_ATTEMPTS = 3


class QQWebClient:
    """HTTP client for the pd.qq.com noauth RPC surface.

    A single instance is safe to reuse; the underlying opener carries
    the cookie jar seeded by :meth:`_init_session`.
    """

    def __init__(self, guild_id: str, guild_number: str, max_workers: int = 10):
        self.guild_id = guild_id
        self.guild_number = guild_number
        self.max_workers = max_workers

        self.cookie_jar = CookieJar()
        cookie_processor = urllib.request.HTTPCookieProcessor(self.cookie_jar)
        self.opener = urllib.request.build_opener(cookie_processor)

        self._common_headers = {
            "x-qq-client-appid": _CLIENT_APPID,
            "Content-Type": "application/json",
            "Referer": f"https://pd.qq.com/g/{self.guild_id}",
        }

        self._init_session()

    def _init_session(self) -> None:
        """GET the guild page once to seed the cookie jar."""
        url = f"https://pd.qq.com/g/{self.guild_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
        logger.debug("session init GET %s", url)
        self.opener.open(req, timeout=30).read()

    def _post(
        self, api_path: str, body: dict[str, Any], service_type: int
    ) -> dict[str, Any]:
        """POST ``body`` to ``{api_path}`` and return the parsed JSON.

        Retries transient HTTP/URL errors with exponential backoff
        (1s, 2s, 4s) up to :data:`_MAX_ATTEMPTS` total attempts. A
        non-zero ``code`` in the response body is logged as a warning
        but not treated as a transport failure — callers handle partial
        payloads via defensive defaults.
        """
        url = f"{_BASE}{api_path}"
        # Backend requires the exact field name `uint32_service_type`.
        headers = dict(self._common_headers)
        headers["x-oidb"] = json.dumps({"uint32_service_type": service_type})
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with self.opener.open(req, timeout=30) as resp:
                    raw = resp.read()
                parsed = json.loads(raw.decode("utf-8"))
                if parsed.get("code") not in (None, 0):
                    logger.warning(
                        "pd.qq.com %s returned code=%s msg=%s",
                        api_path,
                        parsed.get("code"),
                        parsed.get("msg"),
                    )
                return parsed
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                last_exc = exc
                logger.warning(
                    "HTTP error on %s (attempt %d/%d): %s",
                    api_path,
                    attempt,
                    _MAX_ATTEMPTS,
                    exc,
                )
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_RETRY_DELAYS[attempt - 1])

        assert last_exc is not None
        raise last_exc

    def get_feeds(
        self, from_: int = 7, feed_attch_info: str = ""
    ) -> tuple[list, str, bool]:
        """Call ``GetGuildFeeds`` (service_type=12).

        Returns ``(vecFeed, feedAttchInfo, isFinish)`` with safe defaults.
        """
        body = {
            "count": 10,
            "from": from_,
            "guild_id": self.guild_id,
            "get_type": 1,
            "feedAttchInfo": feed_attch_info,
            "sortOption": 0,
            "need_channel_list": False,
            "need_top_info": False,
        }
        resp = self._post("GetGuildFeeds", body, service_type=12)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else None
        data = data or resp
        return (
            data.get("vecFeed", []) or [],
            data.get("feedAttchInfo", "") or "",
            bool(data.get("isFinish", False)),
        )

    def get_guild_channels(self) -> list[dict]:
        """Return all channels of the guild.

        Calls ``GetGuildFeeds`` with ``need_channel_list=True`` and
        returns the ``channels`` array. Empty list on error.
        """
        body = {
            "count": 1,
            "from": 7,
            "guild_id": self.guild_id,
            "get_type": 1,
            "feedAttchInfo": "",
            "sortOption": 0,
            "need_channel_list": True,
            "need_top_info": False,
        }
        resp = self._post("GetGuildFeeds", body, service_type=12)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else None
        return (data or {}).get("channels") or []

    def get_channel_feeds(
        self, channel_id: str, from_: int = 7, feed_attch_info: str = ""
    ) -> tuple[list, str, bool]:
        """Call ``GetChannelTimelineFeeds`` (service_type=11).

        pd.qq.com uses this endpoint for channel-specific feeds (12 per
        page, ``count`` is ignored).  The ``channelSign`` identifies the
        channel and guild.

        Returns ``(vecFeed, feedAttchInfo, isFinish)`` with safe defaults.
        """
        body = {
            "count": 1,
            "from": from_,
            "channelSign": {
                "guild_id": self.guild_id,
                "channel_id": str(channel_id),
            },
            "feedAttchInfo": feed_attch_info,
            "sortOption": 0,
        }
        resp = self._post("GetChannelTimelineFeeds", body, service_type=11)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else None
        data = data or resp
        return (
            data.get("vecFeed", []) or [],
            data.get("feedAttchInfo", "") or "",
            bool(data.get("isFinish", False)),
        )

    def get_feed_comments(
        self, feed_id: str, list_num: int = 20, attch_info: str = ""
    ) -> tuple[list, int, str]:
        """Call ``GetFeedComments`` (service_type=5).

        Returns ``(vecComment, totalNum, attchInfo)`` with safe defaults.
        """
        body = {
            "feedId": feed_id,
            "listNum": list_num,
            "from": 1,
            "src": 0,
            "attchInfo": attch_info,
            "channelSign": {"guild_number": self.guild_number},
            "extInfo": {
                "mapInfo": [
                    {"key": "qc-tabid", "value": ""},
                    {"key": "qc-pageid", "value": ""},
                ]
            },
            "rankingType": 1,
            "replyListNum": 1,
        }
        resp = self._post("GetFeedComments", body, service_type=5)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else None
        data = data or resp
        total = data.get("totalNum", 0)
        try:
            total_int = int(total)
        except (TypeError, ValueError):
            total_int = 0
        return (
            data.get("vecComment", []) or [],
            total_int,
            data.get("attchInfo", "") or "",
        )
