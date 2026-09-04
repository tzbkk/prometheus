"""launcher /targets 家族活体契约测试——MD-071/072/073。"""

import os


def test_live_targets_family_conforms_contracted_schemas(
    launcher_service, http, schema_assert
):
    api = launcher_service()
    base = f"http://127.0.0.1:{api.port}"

    status, body = http("GET", f"{base}/targets")
    assert status == 200
    schema_assert(body, "TargetList")
    assert [t["name"] for t in body["targets"]] == [
        "scraper", "deepbackfill", "viewer",
    ]

    status, body = http("GET", f"{base}/targets/scraper")
    assert status == 200
    schema_assert(body, "TargetStatus")
    assert body == {
        "name": "scraper", "state": "stopped",
        "pid": None, "uptime_sec": None, "restarts": 0,
    }

    api.pm.start("viewer")
    log_path = api.pm.log_path("viewer")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("line-one\nline-two\nline-three\n")

    status, body = http("GET", f"{base}/targets/viewer/logs")
    assert status == 200
    schema_assert(body, "LogTail")
    assert body["lines"] == ["line-one", "line-two", "line-three"]

    status, body = http("GET", f"{base}/targets/viewer/logs?tail=1")
    assert status == 200
    schema_assert(body, "LogTail")
    assert body["lines"] == ["line-three"]

    status, body = http("GET", f"{base}/targets/scraper/logs")
    assert status == 200
    schema_assert(body, "LogTail")
    assert body["lines"] == []


def test_live_target_actions_idempotent_return_current_state(
    launcher_service, http, schema_assert
):
    api = launcher_service()
    base = f"http://127.0.0.1:{api.port}"

    status, first = http("POST", f"{base}/targets/scraper/start")
    assert status == 200
    schema_assert(first, "TargetStatus")
    assert first["state"] == "running"
    assert isinstance(first["pid"], int)
    assert first["restarts"] == 0

    status, again = http("POST", f"{base}/targets/scraper/start")
    assert status == 200
    schema_assert(again, "TargetStatus")
    assert again == first

    status, stopped = http("POST", f"{base}/targets/scraper/stop")
    assert status == 200
    schema_assert(stopped, "TargetStatus")
    assert stopped["state"] == "stopped"

    status, stopped_again = http("POST", f"{base}/targets/scraper/stop")
    assert status == 200
    assert stopped_again == stopped

    status, restarted = http("POST", f"{base}/targets/scraper/restart")
    assert status == 200
    schema_assert(restarted, "TargetStatus")
    assert restarted["state"] == "running"
    assert restarted["pid"] != first["pid"]
    assert restarted["restarts"] == 1


def test_live_unknown_target_and_route_return_error_envelope(
    launcher_service, http, schema_assert
):
    api = launcher_service()
    base = f"http://127.0.0.1:{api.port}"

    status, body = http("GET", f"{base}/targets/qq")
    assert status == 404
    schema_assert(body, "ErrorEnvelope")
    assert body["error"]["code"] == "invalid_target"

    status, body = http("POST", f"{base}/targets/qq/start")
    assert status == 404
    schema_assert(body, "ErrorEnvelope")

    status, body = http("GET", f"{base}/nope")
    assert status == 404
    schema_assert(body, "ErrorEnvelope")
    assert body["error"]["code"] == "not_found"

    status, body = http("GET", f"{base}/targets/scraper/bogus")
    assert status == 404
    schema_assert(body, "ErrorEnvelope")

    status, body = http("PUT", f"{base}/targets")
    assert status == 404
    schema_assert(body, "ErrorEnvelope")
