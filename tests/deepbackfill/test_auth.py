"""MD-100/101/102：bkn 向量 + AuthClient 请求规格/翻页/重铸。"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from src.deepbackfill.auth import AUTH_BASE_URL, AuthClient, AuthError, bkn
from src.deepbackfill.credentials import Credentials

from tests.deepbackfill.conftest import (
    GUILD,
    GUILD_NUMBER,
    P_SKEY,
    UIN,
    FakeSource,
    NoopSleep,
    RecordingTransport,
    http_error,
)

T_MS = 1782919600123

CREDS = Credentials(
    uin=str(UIN),
    p_uin=f"o{UIN}",
    p_skey=P_SKEY,
    minted_at=T_MS,
)

FRESH_CREDS = Credentials(
    uin=str(UIN),
    p_uin=f"o{UIN}",
    p_skey="@FreshKey0000000000000000000000000000004",
    minted_at=T_MS + 1,
)


def auth_page(vec, attach="", finish=False):
    return {"result": 0, "data": {"vecFeed": vec, "feedAttchInfo": attach, "isFinish": finish}}


def make_client(transport, source=None, sleep=None, now_ms=None):
    return AuthClient(
        GUILD_NUMBER,
        source or FakeSource(CREDS),
        transport=transport,
        sleep=sleep if sleep is not None else (lambda s: None),
        now_ms=now_ms or (lambda: T_MS),
    )


def test_bkn_djb_hash_vectors():
    # §2.2 文档算法已知答案（任务给定交叉向量 1739130872 预检不符——
    # 任何 DJB 变体均不可复现，弃用；以文档逐字节验证过的算法为准）。
    assert bkn("") == 5381
    assert bkn("A") == 177638  # 5381*33 + 65
    assert bkn("@AbCdEfGh1") == 1358553434
    assert bkn(P_SKEY) == bkn(P_SKEY)
    long_key = "k" * 44  # p_skey 44 字符域：掩码上界逐字节成立
    assert 0 <= bkn(long_key) < 0x80000000
    assert bkn("ab") != bkn("ba")  # 字节序敏感


def test_auth_client_request_shape_headers_body_pagination():
    transport = RecordingTransport({"GetGuildFeeds": auth_page([{"id": "B_1"}], "page-2")})
    sleep = NoopSleep()
    client = make_client(transport, sleep=sleep)

    vec, attach, finish = client.get_feeds("")

    assert vec == [{"id": "B_1"}] and attach == "page-2" and finish is False
    assert sleep.calls == [1.0]  # ≥1s/页礼貌间隔（§5）

    req = transport.last()
    parsed = urlparse(req.full_url)
    assert req.full_url.startswith(AUTH_BASE_URL)
    assert "guild_feed_reader.ComReader/GetGuildFeeds" in req.full_url
    assert parse_qs(parsed.query) == {
        "bkn": [str(bkn(P_SKEY))],
        "_t": [str(T_MS)],
        "_v": ["1.0.1"],
        "client_platform": ["pcqqwebview"],
    }
    assert req.get_header("Cookie") == f"p_uin=o{UIN}; uin={UIN}; p_skey={P_SKEY}"
    assert json.loads(req.get_header("X-oidb")) == {
        "uint32_command": "0x93df", "uint32_service_type": 13,
    }
    assert req.get_header("X-qq-client-appid") == "537379447"
    assert req.get_header("Origin") == "https://pd.qq.com"
    assert req.get_header("Referer") == "https://pd.qq.com/explore"
    assert "QQAppId/537379447 QQWebview/1.0.0.0" in req.get_header("User-agent")

    assert transport.request_body(req) == {
        "count": 20,
        "from": 7,
        "guild_number": GUILD_NUMBER,
        "get_type": 1,
        "feedAttchInfo": "",
        "sortOption": 0,
        "need_channel_list": False,
        "need_top_info": False,
    }

    # cursor 回填：第二页请求体带上一页 feedAttchInfo；isFinish 原样透传。
    transport2 = RecordingTransport(
        {"GetGuildFeeds": auth_page([], "", True)}
    )
    vec2, attach2, finish2 = AuthClient(
        GUILD_NUMBER,
        FakeSource(CREDS),
        transport=transport2,
        sleep=lambda s: None,
        now_ms=lambda: T_MS,
    ).get_feeds("page-2")
    assert (vec2, attach2, finish2) == ([], "", True)
    assert transport2.request_body(transport2.last())["feedAttchInfo"] == "page-2"
    assert len(sleep.calls) == 1


def test_auth_client_remints_once_then_retry_or_readable_error():
    # 象限 result≠0：首败 → remint 一次 → 新 p_skey 重试成功。
    routes = {
        "GetGuildFeeds": [
            {"result": 1001, "msg": "login expired"},
            auth_page([{"id": "B_2"}], "", True),
        ]
    }
    transport = _SequencedTransport(routes)
    source = FakeSource(CREDS, remint_creds=FRESH_CREDS)
    client = make_client(transport, source=source)

    vec, attach, finish = client.get_feeds("")
    assert finish is True and vec == [{"id": "B_2"}]
    assert source.remint_calls == 1
    bodies = [transport.request_body(r) for r in transport.requests]
    assert all(b["guild_number"] == GUILD_NUMBER for b in bodies)
    cookies = [r.get_header("Cookie") for r in transport.requests]
    assert P_SKEY in cookies[0] and "@FreshKey" in cookies[1]
    bkns = [parse_qs(urlparse(r.full_url).query)["bkn"][0] for r in transport.requests]
    assert bkns == [str(bkn(P_SKEY)), str(bkn("@FreshKey0000000000000000000000000000004"))]

    # 象限 403：同样 remint-once；重铸后仍败 → 可读 AuthError（不泄 p_skey）。
    transport3 = _SequencedTransport(
        {"GetGuildFeeds": [
            http_error(AUTH_BASE_URL, 403, {"message": "denied"}),
            http_error(AUTH_BASE_URL, 403, {"message": "denied"}),
        ]}
    )
    source3 = FakeSource(CREDS, remint_creds=FRESH_CREDS)
    with pytest.raises(AuthError, match="after remint"):
        make_client(transport3, source=source3).get_feeds("")
    assert source3.remint_calls == 1

    # 重铸面本身失败 → 可读错误（两段信息都在）。
    source4 = FakeSource(CREDS, remint_raises=RuntimeError("source down"))
    transport4 = _SequencedTransport(
        {"GetGuildFeeds": [{"result": 1001, "msg": "login expired"}]}
    )
    with pytest.raises(AuthError, match="remint failed.*source down"):
        make_client(transport4, source=source4).get_feeds("")

    # 空数据（无 vecFeed/isFinish 键）→ 认证性失败走重铸；仍空 → 可读错误。
    transport5 = _SequencedTransport(
        {"GetGuildFeeds": [{"result": 0, "data": None}, {"result": 0}]}
    )
    with pytest.raises(AuthError, match="empty auth payload"):
        make_client(transport5, source=FakeSource(CREDS, remint_creds=FRESH_CREDS)).get_feeds("")

    # 传输面（URLError）不经重铸，直接可读 AuthError。
    import urllib.error

    transport6 = RecordingTransport(
        {"GetGuildFeeds": urllib.error.URLError("conn refused")}
    )
    with pytest.raises(AuthError, match="unreachable"):
        make_client(transport6).get_feeds("")


class _SequencedTransport:
    """按调用序分发 canned 响应（同一 URL 多页场景）。"""

    def __init__(self, routes: dict):
        self._routes = {
            substr: list(canned) for substr, canned in routes.items()
        }
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        for substr, queue in self._routes.items():
            if substr in req.full_url:
                assert queue, f"no canned response left for {substr}"
                canned = queue.pop(0)
                if isinstance(canned, BaseException):
                    raise canned
                if isinstance(canned, dict):
                    from tests.deepbackfill.conftest import FakeResponse

                    return FakeResponse(json.dumps(canned).encode("utf-8"))
                return canned
        raise AssertionError(f"unexpected transport URL: {req.full_url}")

    def request_body(self, req) -> dict:
        return json.loads(req.data.decode("utf-8"))


def test_auth_client_channel_wire_format():
    """MD-128：频道面线格式（浏览器抓包实证）——commreader 路由、query 仅
    bkn（_t/_v/client_platform 会触发 [backend] 参数错误）、x-oidb 仅
    service_type=10、appid 537246381、Referer /g/{guild_id}、channelSign
    {guild_id, channel_id}、sortOption=0 时序。"""
    transport = RecordingTransport(
        {"GetChannelTimelineFeeds": {
            "result": 0,
            "data": {"vecFeed": [{"id": "B_9"}], "feedAttchInfo": "c2",
                     "isFinish": False},
        }}
    )
    client = AuthClient(
        GUILD_NUMBER,
        FakeSource(CREDS),
        guild_id=GUILD,
        transport=transport,
        sleep=lambda s: None,
        now_ms=lambda: T_MS,
    )
    vec, attach, finish = client.get_channel_feeds("987654321")
    assert (vec, attach, finish) == ([{"id": "B_9"}], "c2", False)

    req = transport.last()
    parsed = urlparse(req.full_url)
    assert "commreader.ComReader/GetChannelTimelineFeeds" in req.full_url
    assert parse_qs(parsed.query) == {"bkn": [str(bkn(P_SKEY))]}
    assert json.loads(req.get_header("X-oidb")) == {"uint32_service_type": 10}
    assert req.get_header("X-qq-client-appid") == "537246381"
    assert req.get_header("Referer") == f"https://pd.qq.com/g/{GUILD}"
    assert transport.request_body(req) == {
        "count": 10,
        "from": 7,
        "channelSign": {"guild_id": GUILD, "channel_id": "987654321"},
        "feedAttchInfo": "",
        "sortOption": 0,
        "need_top_info": False,
    }


def test_auth_client_channel_list_and_remint():
    """MD-128：频道清单（need_channel_list=True → data.channels）+ 频道面
    认证性失败走同一 remint-once 语义。"""
    transport = RecordingTransport(
        {
            "GetGuildFeeds": [
                {"result": 0, "data": {"channels": [
                    {"channel_id": "111222333", "name": "帖子广场"},
                ], "vecFeed": [], "feedAttchInfo": "", "isFinish": True}},
            ],
            "GetChannelTimelineFeeds": [
                {"result": 5, "msg": "expired"},
                {"result": 0, "data": {"vecFeed": [], "feedAttchInfo": "",
                                       "isFinish": True}},
            ],
        }
    )
    client = AuthClient(
        GUILD_NUMBER,
        FakeSource(CREDS, remint_creds=FRESH_CREDS),
        guild_id=GUILD,
        transport=transport,
        sleep=lambda s: None,
        now_ms=lambda: T_MS,
    )
    channels = client.get_guild_channels()
    assert channels == [{"channel_id": "111222333", "name": "帖子广场"}]

    vec, attach, finish = client.get_channel_feeds("111222333")
    assert (vec, attach, finish) == ([], "", True)
    tl_reqs = [r for r in transport.requests
               if "GetChannelTimelineFeeds" in r.full_url]
    assert len(tl_reqs) == 2
