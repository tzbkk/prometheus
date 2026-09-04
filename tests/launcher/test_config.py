"""launcher GET/PUT /config 活体契约测试——MD-074（ 回显双向）。"""

import json
import urllib.error
import urllib.request


def test_live_config_get_put_echoes_merged_full_object(
    launcher_service, http, schema_assert, tmp_path
):
    config_path = str(tmp_path / "launcher.conf.json")
    api = launcher_service(config_path=config_path)
    base = f"http://127.0.0.1:{api.port}"

    status, body = http("GET", f"{base}/config")
    assert status == 200
    schema_assert(body, "LauncherConfig")
    assert body["guilds"] == [{"guild_id": "1000000000000001"}]
    assert body["max_restarts"] == 5

    status, body = http("PUT", f"{base}/config", {"max_restarts": 9})
    assert status == 200
    schema_assert(body, "LauncherConfig")
    assert body["max_restarts"] == 9
    assert body["guilds"] == [{"guild_id": "1000000000000001"}]
    assert body["launcher_port"] == 9421

    with open(config_path, encoding="utf-8") as fh:
        persisted = json.load(fh)
    assert persisted["max_restarts"] == 9
    assert "guilds" not in persisted

    status, body = http("PUT", f"{base}/config", {"guilds": []})
    assert status == 400
    schema_assert(body, "ErrorEnvelope")
    assert body["error"]["code"] == "guilds_readonly"

    req = urllib.request.Request(
        f"{base}/config", data=b"{not json", method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    assert status == 400
    body = json.loads(raw.decode("utf-8"))
    schema_assert(body, "ErrorEnvelope")
    assert body["error"]["code"] == "bad_request"
