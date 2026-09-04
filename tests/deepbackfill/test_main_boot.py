"""入口装配冒烟（MD-115）——main() 焊点。

入口 wiring 必须有子进程级冒烟：组件级测试全绿 ≠ 装配绿
（`_build_components` 签名与 `main()` 调用的漂移只有子进程能暴露）。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

_GUILD = "9990000000000099"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_main_entry_boots_answers_health_and_drains_on_sigterm(tmp_path):
    tmp = str(tmp_path)
    os.makedirs(os.path.join(tmp, "data", _GUILD))
    conf = {
        "guilds": [{"guild_id": _GUILD, "guild_number": "bootguild"}],
        "data_dir": os.path.join(tmp, "data"),
        "scraper_max_workers": 2,
    }
    conf_path = os.path.join(tmp, "prometheus.conf.json")
    with open(conf_path, "w") as f:
        json.dump(conf, f)
    env = dict(
        os.environ,
        PROMETHEUS_CONFIG=conf_path,
        PROMETHEUS_DEEPBACKFILL_CONF=os.path.join(tmp, "deepbackfill.conf.json"),
    )
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.deepbackfill", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        + os.sep
        + os.pardir,
    )
    try:
        body = None
        for _ in range(60):  # ≤15s 开机预算
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1
                ) as resp:
                    assert resp.status == 200
                    body = json.loads(resp.read().decode())
                    break
            except Exception:
                if proc.poll() is not None:
                    out = proc.stdout.read().decode("utf-8", "replace")
                    raise AssertionError(
                        f"main() died at boot (rc={proc.returncode}):\n{out[-800:]}"
                    )
        assert body is not None, "service never came up within 15s"
        assert body == {"scanned_feeds": 0}
    finally:
        if proc.poll() is None:
            subprocess.run(["kill", str(proc.pid)], check=False)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise AssertionError("SIGTERM drain hung — boot smoke failed")
    assert proc.returncode == 0
