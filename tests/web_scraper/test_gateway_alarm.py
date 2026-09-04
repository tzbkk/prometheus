"""网关拒绝可见性（僵尸绿灯防线）。

背景：noauth 网关若被锁（retcode 150 "no privilege"），
200 body 内 code!=0 只记 WARNING → 列表返回空 vecFeed → daemon 视作
"没有新帖"照常完成周期——进程绿灯、周期照跑、静默零捕获。防线 =
client 逐次计数（gateway_rejects）+ daemon 周期末升 ERROR 并入 /stats。
"""

from __future__ import annotations

import io
import json
import logging

from tests.web_scraper.conftest import (
    GUILD,
    FakeClient,
    build_guild_context,
    synthetic_feed,
)


def _rejected_client(code: int):
    from src.web_scraper.client import QQWebClient

    client = QQWebClient.__new__(QQWebClient)
    client.guild_id = GUILD
    client.guild_number = "Takagi3channel"
    client.max_workers = 1
    client.gateway_rejects = 0
    client._common_headers = {
        "x-qq-client-appid": "537246381",
        "Content-Type": "application/json",
        "Referer": f"https://pd.qq.com/g/{GUILD}",
    }

    body = json.dumps({"code": code, "msg": "no privilege"}).encode("utf-8")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _open(req, timeout=None):
        return _Resp(body)

    class _Opener:
        open = staticmethod(_open)

    client.opener = _Opener()
    return client


def test_client_counts_gateway_rejects():
    """MD-134：code!=0 的 200 body → gateway_rejects 逐次 +1；transport 失败不计。"""
    client = _rejected_client(150)
    for _ in range(3):
        vec, attach, finish = client.get_feeds()
        assert vec == [] and attach == "" and finish is False
    assert client.gateway_rejects == 3

    vec, _, _ = client.get_channel_feeds("635032487")
    assert vec == []
    assert client.gateway_rejects == 4


def test_daemon_escalates_gateway_rejects(tmp_path, caplog):
    """MD-135：周期内出现网关拒绝 → ERROR 级日志 + /stats gateway_rejects 可见。"""

    class LockedClient(FakeClient):
        gateway_rejects = 0

        def get_feeds(self, from_: int = 7, feed_attch_info: str = ""):
            self.gateway_rejects += 2
            return ([], "", True)

        def get_channel_feeds(
            self, channel_id: str, from_: int = 7, feed_attch_info: str = ""
        ):
            self.gateway_rejects += 1
            return ([], "", True)

    client = LockedClient([synthetic_feed("B_locked_probe")], {})
    harness = build_guild_context(tmp_path, client)
    with caplog.at_level(logging.ERROR, logger="src.web_scraper.daemon"):
        harness.daemon.run_once()

    assert harness.stats["gateway_rejects"] == 3
    assert harness.stats["guilds"][GUILD]["gateway_rejects"] == 3
    escalated = [
        r for r in caplog.records if "gateway rejected" in r.getMessage()
    ]
    assert escalated, "gateway rejects must escalate to ERROR"
    assert escalated[0].levelname == "ERROR"
    assert GUILD in escalated[0].getMessage()
