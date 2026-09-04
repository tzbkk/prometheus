"""harness demo：viewer 起临时端口。

/api/stats 已契约化：demo 断言为编译 Schema（ViewerStats 单命名三键，㉒-d）。
"""

import threading

import pytest

from src.viewer.backend.server import ViewerServer


@pytest.fixture
def live_viewer(tmp_path):
    server = ViewerServer(port=0, db_path=str(tmp_path / "viewer.db"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_harness_viewer_serves_api_stats_on_temp_port(live_viewer, http, schema_assert):
    assert live_viewer.port > 0
    status, body = http("GET", f"http://127.0.0.1:{live_viewer.port}/api/stats")
    assert status == 200
    schema_assert(body, "ViewerStats")  # ViewerStats 边
    assert body == {"feeds": 0, "comments": 0, "media": 0}  # 无 data_dir → 空索引
