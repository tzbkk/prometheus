""":9420 五端点活体 mandate（tests/harness 惯例，port=0 临时端口）。

MD-049 正例：五指令边响应逐一过编译 Schema（ScraperHealth/ScraperStats/
ScraperConfig/ScraperLogs/ScraperAction）；PUT /config ㉖ 回显形态（部分
字段 → 合并 → 新态全文）。
MD-050 负例：未知路由 404 + ErrorEnvelope；PUT /config 畸形体（坏 JSON/
非对象/guilds 键）400 + ErrorEnvelope。
MD-051 harness 执法负演示：stats 字段类型错造（手工 mock）→ Schema 断言
红（passthrough 不遮蔽——错造必须可被 harness 捕获）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from src.web_scraper.api_server import ScraperService

from tests.web_scraper.conftest import GUILD, GUILD_NUMBER


@pytest.fixture
def live_service():
    stats = {
        "scanned_feeds": 3,
        "feeds": 2,
        "comments": 4,
        "replies": 1,
        "media": 2,
        "last_scan_ts": 0,
        "daemon_running": False,
        "guilds": {},
        "log_buffer": [
            {"seq": 1, "level": "INFO", "msg": "scan cycle complete", "ts": "..."},
            {"seq": 2, "level": "WARNING", "msg": "media retry", "ts": "..."},
        ],
    }
    config_state = {
        "apiVersion": "2",
        "guilds": [
            {"guild_id": GUILD, "guild_number": GUILD_NUMBER, "name": "合成频道"}
        ],
        "scraper_max_workers": 10,
        "scraper_daemon_interval_sec": 120,
        "scraper_api_port": 9420,
    }
    fired = []
    service = ScraperService(
        stats=stats,
        config_state=config_state,
        trigger_callback=lambda: fired.append(True),
        port=0,
    )
    service.start()
    try:
        yield SimpleNamespace(
            service=service, stats=stats, fired=fired, base=f"http://127.0.0.1:{service.port}"
        )
    finally:
        service.stop()


def test_live_five_endpoints_conform_contracted_schemas(live_service, http, schema_assert):
    base = live_service.base

    status, body = http("GET", f"{base}/health")
    assert status == 200
    schema_assert(body, "ScraperHealth")
    assert body["scanned_feeds"] == 3

    status, body = http("GET", f"{base}/stats")
    assert status == 200
    schema_assert(body, "ScraperStats")
    assert body == {
        "feeds": 2,
        "comments": 4,
        "replies": 1,
        "media": 2,
        "gateway_rejects": 0,
    }

    status, body = http("GET", f"{base}/config")
    assert status == 200
    schema_assert(body, "ScraperConfig")
    assert body["guilds"][0]["guild_id"] == GUILD

    status, body = http(
        "PUT", f"{base}/config", {"scraper_daemon_interval_sec": 60}
    )
    assert status == 200
    schema_assert(body, "ScraperConfig")  # ㉖：回显 = 合并后的新态全文
    assert body["scraper_daemon_interval_sec"] == 60
    assert body["guilds"][0]["guild_id"] == GUILD  # 未提交字段全保留

    status, body = http("GET", f"{base}/logs")
    assert status == 200
    schema_assert(body, "ScraperLogs")
    assert body["lines"] == ["[INFO] scan cycle complete", "[WARNING] media retry"]

    status, body = http("GET", f"{base}/logs?tail=1")
    assert status == 200
    assert body["lines"] == ["[WARNING] media retry"]

    for _ in range(2):  # 幂等：重复受理亦 true
        status, body = http("POST", f"{base}/action/trigger-daemon")
        assert status == 200
        schema_assert(body, "ScraperAction")
        assert body["triggered"] is True
    deadline = time.time() + 2
    while not live_service.fired and time.time() < deadline:
        time.sleep(0.01)
    assert live_service.fired


def test_live_unknown_route_and_malformed_config_put_return_error_envelope(
    live_service, http, schema_assert
):
    base = live_service.base

    status, body = http("GET", f"{base}/nope")
    assert status == 404
    schema_assert(body, "ErrorEnvelope")
    assert body["error"]["code"] == "not_found"

    status, body = http("PUT", f"{base}/config", [1, 2, 3])
    assert status == 400
    schema_assert(body, "ErrorEnvelope")
    assert body["error"]["code"] == "bad_request"

    request = urllib.request.Request(
        f"{base}/config", data=b"{not valid json", method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=5)
        raised = None
    except urllib.error.HTTPError as exc:
        raised = exc

    assert raised is not None and raised.code == 400
    envelope = json.loads(raised.read())
    schema_assert(envelope, "ErrorEnvelope")
    assert envelope["error"]["code"] == "bad_request"

    status, body = http("PUT", f"{base}/config", {"guilds": []})
    assert status == 400
    schema_assert(body, "ErrorEnvelope")
    assert body["error"]["code"] == "guilds_readonly"

    status, body = http("DELETE", f"{base}/config")
    assert status == 404
    schema_assert(body, "ErrorEnvelope")


def test_live_harness_red_on_wrong_typed_stats_mock(live_service, http, schema_assert):
    live_service.stats["feeds"] = "many"  # 手工 mock：字段类型错造
    status, body = http("GET", f"{live_service.base}/stats")
    assert status == 200  # passthrough：错造直达响应面
    with pytest.raises(AssertionError):
        schema_assert(body, "ScraperStats")  # harness 必红——执法不被遮蔽
