"""launcher POST /shutdown 活体契约测试——MD-075（ShutdownAck）。"""

import time


def test_live_shutdown_accepts_and_stops_all_targets(
    launcher_service, http, schema_assert
):
    callbacks = []
    api = launcher_service(shutdown_callback=lambda: callbacks.append(True))
    base = f"http://127.0.0.1:{api.port}"

    for name in ("scraper", "deepbackfill", "viewer"):
        api.pm.start(name)
    assert all(
        t["state"] == "running" for t in api.pm.status_all()["targets"]
    )

    status, body = http("POST", f"{base}/shutdown")
    assert status == 200
    schema_assert(body, "ShutdownAck")
    assert body == {"accepted": True}

    deadline = time.time() + 5
    while time.time() < deadline and not callbacks:
        time.sleep(0.05)
    assert callbacks == [True]

    snapshot = api.pm.status_all()
    assert [t["state"] for t in snapshot["targets"]] == ["stopped"] * 3
    assert api.pm.processes == {}
