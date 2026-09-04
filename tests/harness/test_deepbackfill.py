"""deepbackfill 服务活体验证（实现态）。

/health 真计数过编译 Schema（pillar 对应面）；契约其余四指令边真实现
（stats/config/logs/trigger 各自的深度执法在
tests/deepbackfill/test_service.py）。
"""

import pytest

from src.deepbackfill.service import DeepbackfillService

_IMPLEMENTED_ROUTES = (
    ("GET", "/stats"),
    ("GET", "/config"),
    ("PUT", "/config"),
    ("GET", "/logs"),
    ("POST", "/action/trigger-daemon"),
)


@pytest.fixture
def live_skeleton():
    service = _live_service()
    try:
        yield service
    finally:
        service.stop()


def _live_service():
    service = DeepbackfillService(
        stats={
            "scanned_feeds": 0,
            "pages": 0,
            "feeds": 0,
            "comments": 0,
            "replies": 0,
            "media": 0,
            "running": False,
            "log_buffer": [],
        },
        config_state={
            "apiVersion": "2",
            "guilds": [{"guild_id": "7743321643036658"}],
            "deepbackfill_api_port": 9424,
        },
        trigger=lambda: True,
        port=0,
    )
    service.start()
    return service


def test_harness_deepbackfill_health_conforms_schema(live_skeleton, http, schema_assert):
    assert live_skeleton.port > 0
    status, body = http("GET", f"http://127.0.0.1:{live_skeleton.port}/health")
    assert status == 200
    schema_assert(body, "DeepbackfillHealth")
    assert body["scanned_feeds"] == 0  # 空跑真计数（runner 未触发）


def test_harness_deepbackfill_edges_implemented(http, schema_assert):
    service = _live_service()
    try:
        base = f"http://127.0.0.1:{service.port}"
        for method, route in _IMPLEMENTED_ROUTES:
            status, body = http(method, base + route)
            assert status != 501, f"{method} {route} endpoint must be real (implemented), not a stub"
            if status >= 400:
                schema_assert(body, "ErrorEnvelope")
    finally:
        service.stop()
