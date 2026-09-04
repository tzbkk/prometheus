"""auth 通道获取器：guild_feed_reader 全史翻页客户端。

规格圣经 doc/DEEP_BACKFILL.md §3 逐字：

- 请求（§2.1）：POST
  ``https://pd.qq.com/qunng/guild/gotrpc/auth/trpc.qchannel.guild_feed_reader.ComReader/GetGuildFeeds
  ?bkn=<bkn>&_t=<epoch_ms>&_v=1.0.1&client_platform=pcqqwebview``；
  cookie ``p_uin=o<uin>; uin=<uin>; p_skey=<pskey>``；头
  ``x-oidb: {"uint32_command":"0x93df","uint32_service_type":13}``、
  ``x-qq-client-appid: 537379447``（webview 专用，≠ Linux 客户端 537376650）、
  origin/referer/user-agent 照文档；body ``{"count":20,"from":7,
  "guild_number":"<guild_number>","get_type":1,"feedAttchInfo":"<cursor>",
  "sortOption":0,"need_channel_list":false,"need_top_info":false}``。
- bkn 自算（§2.2，经典 QZONE 哈希逐字节验证）：
  ``h=5381; h=(h+(h<<5)+ord(c))&0x7FFFFFFF``（与 weblogin.hash33(s, 5381)
  同函数——ptlogin2 家族哈希的 bkn 面）。
- 翻页（§2.3）：cursor=data.feedAttchInfo 回填、isFinish==true 干净终止、
  游标不变=异常终止（终止判定归 runner 循环；本客户端原样返回三元组）。
- 礼貌间隔 ≥1s/页（§5 风控护栏；实测 1.13s/页无风控迹象）。
- 认证失败（result≠0/401/403/空数据）→ 凭据源 remint() 重试一次（纯网
  路线下 CredentialManager.remint 恒抛可读错误——重登录归服务端
  QR 会话，本路径收敛为可读 AuthError），仍败 = 可读 AuthError。

与 noauth 的差异（§2.1 表）：服务名 guild_feed_reader（非 commreader）、
body 键 guild_number（非 guild_id）、x-oidb 0x93df+13（非 12）、全史无窗。

credential_source 缝：``credentials() -> Credentials`` +
``remint() -> Credentials``（credentials.CredentialManager 实现）——
get_feeds 每页现取凭据（重铸后即刻生效）。transport 缝同 credentials 模块
（默认 urlopen，测试注入伪 transport）。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from src.deepbackfill.credentials import Credentials, mask_secret

__all__ = ["AUTH_BASE_URL", "AuthClient", "AuthError", "bkn"]

logger = logging.getLogger(__name__)


def _mask(p_skey: str) -> str:
    return mask_secret(p_skey)

AUTH_BASE_URL = (
    "https://pd.qq.com/qunng/guild/gotrpc/auth/"
    "trpc.qchannel.guild_feed_reader.ComReader/GetGuildFeeds"
)
CHANNEL_TIMELINE_URL = (
    "https://pd.qq.com/qunng/guild/gotrpc/auth/"
    "trpc.qchannel.commreader.ComReader/GetChannelTimelineFeeds"
)

_CLIENT_APPID = "537379447"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 QQAppId/537379447 QQWebview/1.0.0.0"
)
_OIDB_HEADER = json.dumps(
    {"uint32_command": "0x93df", "uint32_service_type": 13}, sort_keys=False
)
# 频道 timeline 走 commreader 服务域（浏览器抓包实测）：x-oidb 仅
# service_type=10、appid 537246381、query 只带 bkn——多一个参数即报
# [backend] 参数错误（_t/_v/client_platform 都不能带）。
_CHANNEL_OIDB_HEADER = json.dumps(
    {"uint32_service_type": 10}, sort_keys=False
)
_CHANNEL_APPID = "537246381"
PAGE_INTERVAL_SEC = 1.0  # §5 礼貌间隔下限（实测 1.13s/页含此间隔）
_REQUEST_TIMEOUT = 30.0


def bkn(p_skey: str) -> int:
    """§2.2 逐字：DJB 变体（h*33+c），每步掩 0x7FFFFFFF。"""
    h = 5381
    for c in p_skey:
        h = (h + (h << 5) + ord(c)) & 0x7FFFFFFF
    return h


class AuthError(RuntimeError):
    """auth 通道失败（重铸后仍败/凭据缺失/传输面）——可读错误，绝不夹带 p_skey。"""


class _AuthFailure(Exception):
    """认证性失败（可重铸重试）：result≠0 / 401 / 403 / 空数据载荷。"""


class AuthClient:
    """auth GetGuildFeeds 客户端——签名风格与 noauth QQWebClient.get_feeds 同族
    （``(vecFeed, feedAttchInfo, isFinish)`` 三元组），runner 复用增长机器。
    """

    def __init__(
        self,
        guild_number: str,
        credential_source,
        *,
        guild_id: str | None = None,
        transport=None,
        sleep=time.sleep,
        now_ms=None,
        page_interval: float = PAGE_INTERVAL_SEC,
    ):
        """Args:
            guild_number: 频道 slug（body 键 guild_number 的值——§2.1 实测）。
            credential_source: credentials()/remint() 凭据源（CredentialManager）。
            guild_id: 数字 guild id（channelSign 用；频道 timeline 必填）。
            transport: ``transport(req, timeout=...) -> response``（默认 urlopen）。
            sleep: 间隔缝（测试注入；生产 time.sleep）。
            now_ms: () -> int epoch ms（_t 参数；缺省墙钟）。
            page_interval: 礼貌间隔秒（§5 下限 1.0；测试可缩）。
        """
        self.guild_number = guild_number
        self.guild_id = guild_id
        self._source = credential_source
        self._transport = transport or urllib.request.urlopen
        self._sleep = sleep
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._page_interval = page_interval

    def get_feeds(
        self, feed_attch_info: str = ""
    ) -> tuple[list, str, bool]:
        """一页 auth feeds → ``(vecFeed, feedAttchInfo, isFinish)``。

        认证性失败先经 credential_source.remint() 重铸一次再重试（纯网
        路线下 remint 无本地通道——收敛为可读 AuthError，重登录归服务端 QR
        会话）；仍败 → AuthError。礼貌间隔在每页请求前生效。
        """
        return self._remint_retry(
            lambda creds: self._get_feeds_once(creds, feed_attch_info)
        )

    def get_guild_channels(self) -> list[dict]:
        """频道列表（GetGuildFeeds need_channel_list=True）→ ``channels``。

        GetGuildFeeds 默认流只覆盖默认频道（帖子广场）——逐频道全量走
        timeline 之前必须先拿频道清单。
        """
        data = self._remint_retry(lambda creds: self._guild_channels_once(creds))
        channels = data.get("channels")
        if not isinstance(channels, list):
            raise AuthError("channel list payload has no channels array")
        return channels

    def get_channel_feeds(
        self, channel_id: str, feed_attch_info: str = ""
    ) -> tuple[list, str, bool]:
        """一页频道 timeline → ``(vecFeed, feedAttchInfo, isFinish)``。

        commreader 服务域（x-oidb service_type=10 / appid 537246381 /
        query 仅 bkn——抓包实测）；sortOption=0 时序稳定翻页。
        """
        return self._remint_retry(
            lambda creds: self._channel_feeds_once(
                creds, channel_id, feed_attch_info
            )
        )

    def _remint_retry(self, op):
        creds = self._source.credentials()
        try:
            return op(creds)
        except _AuthFailure as first:
            logger.warning(
                "auth channel rejected credentials (p_skey %s): %s — reminting once",
                _mask(creds.p_skey),
                first,
            )
            try:
                creds = self._source.remint()
            except Exception as exc:
                raise AuthError(
                    f"auth channel failed ({first}) and remint failed: {exc}"
                ) from exc
            try:
                return op(creds)
            except _AuthFailure as second:
                raise AuthError(
                    f"auth channel failed after remint (p_skey {_mask(creds.p_skey)}): {second}"
                ) from second

    def _post_json(self, url: str, query: str, body: bytes, headers: dict) -> dict:
        req = urllib.request.Request(
            f"{url}?{query}", data=body, headers=headers, method="POST"
        )
        try:
            resp = self._transport(req, timeout=_REQUEST_TIMEOUT)
            with resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise _AuthFailure(f"HTTP {exc.code} from auth endpoint") from exc
            raise AuthError(f"auth endpoint HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AuthError(f"auth endpoint unreachable: {exc}") from exc

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuthError(f"auth endpoint returned non-JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise AuthError(
                f"auth endpoint returned {type(parsed).__name__}, expected object"
            )
        result = parsed.get("result")
        if result not in (None, 0):
            raise _AuthFailure(
                f"result={result!r} ({parsed.get('msg') or 'no message'})"
            )
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise _AuthFailure("empty auth payload (no data object)")
        return data

    def _base_headers(self, appid: str, oidb: str, referer: str) -> dict:
        headers = {
            "x-oidb": oidb,
            "x-qq-client-appid": appid,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://pd.qq.com",
            "Referer": referer,
            "User-Agent": _USER_AGENT,
        }
        return headers

    def _guild_channels_once(self, creds: Credentials) -> dict:
        from urllib.parse import urlencode

        self._sleep(self._page_interval)
        params = urlencode(
            {
                "bkn": bkn(creds.p_skey),
                "_t": self._now_ms(),
                "_v": "1.0.1",
                "client_platform": "pcqqwebview",
            }
        )
        body = json.dumps(
            {
                "count": 1,
                "from": 7,
                "guild_number": self.guild_number,
                "get_type": 1,
                "feedAttchInfo": "",
                "sortOption": 0,
                "need_channel_list": True,
                "need_top_info": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = self._base_headers(
            _CLIENT_APPID, _OIDB_HEADER, "https://pd.qq.com/explore"
        )
        headers["Cookie"] = (
            f"p_uin={creds.p_uin}; uin={creds.uin}; p_skey={creds.p_skey}"
        )
        return self._post_json(AUTH_BASE_URL, params, body, headers)

    def _channel_feeds_once(
        self, creds: Credentials, channel_id: str, feed_attch_info: str
    ) -> tuple[list, str, bool]:
        from urllib.parse import urlencode

        self._sleep(self._page_interval)
        query = urlencode({"bkn": bkn(creds.p_skey)})
        body = json.dumps(
            {
                "count": 10,
                "from": 7,
                "channelSign": {
                    "guild_id": self.guild_id,
                    "channel_id": str(channel_id),
                },
                "feedAttchInfo": feed_attch_info,
                "sortOption": 0,
                "need_top_info": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        referer = (
            f"https://pd.qq.com/g/{self.guild_id}"
            if self.guild_id
            else "https://pd.qq.com/explore"
        )
        headers = self._base_headers(_CHANNEL_APPID, _CHANNEL_OIDB_HEADER, referer)
        headers["Cookie"] = (
            f"p_uin={creds.p_uin}; uin={creds.uin}; p_skey={creds.p_skey}"
        )
        data = self._post_json(CHANNEL_TIMELINE_URL, query, body, headers)
        return (
            data.get("vecFeed") or [],
            data.get("feedAttchInfo") or "",
            bool(data.get("isFinish", False)),
        )

    def _get_feeds_once(
        self, creds: Credentials, feed_attch_info: str
    ) -> tuple[list, str, bool]:
        from urllib.parse import urlencode

        self._sleep(self._page_interval)

        params = urlencode(
            {
                "bkn": bkn(creds.p_skey),
                "_t": self._now_ms(),
                "_v": "1.0.1",
                "client_platform": "pcqqwebview",
            }
        )
        body = json.dumps(
            {
                "count": 20,
                "from": 7,
                "guild_number": self.guild_number,
                "get_type": 1,
                "feedAttchInfo": feed_attch_info,
                "sortOption": 0,
                "need_channel_list": False,
                "need_top_info": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = self._base_headers(
            _CLIENT_APPID, _OIDB_HEADER, "https://pd.qq.com/explore"
        )
        headers["Cookie"] = (
            f"p_uin={creds.p_uin}; uin={creds.uin}; p_skey={creds.p_skey}"
        )
        data = self._post_json(AUTH_BASE_URL, params, body, headers)
        if "vecFeed" not in data and "isFinish" not in data:
            raise _AuthFailure("empty auth payload (no data.vecFeed/isFinish)")
        return (
            data.get("vecFeed") or [],
            data.get("feedAttchInfo") or "",
            bool(data.get("isFinish", False)),
        )
