"""launcher 客户端（守护/客户端分离）。

守护进程（``python -m src.launcher --daemon``）是监督树的唯一持久属主；
交互 shell 是瘦客户端——经本模块言 :9421 守护 API。任何新开的
launcher shell 探活即连上同一棵树（docker 模型：daemon 常驻，
client 即开即用）。

线格式：ErrorEnvelope（``{"error": {"code", "message"}}``）→
LauncherClientError（带 daemon message）；不可达 → alive()=False。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

__all__ = ["LauncherClient", "LauncherClientError"]

_LOG_PATHS = {
    "scraper": os.path.join("log", "web_scraper", "scraper.log"),
    "deepbackfill": os.path.join("log", "deepbackfill", "deepbackfill.log"),
    "viewer": os.path.join("log", "viewer", "viewer.log"),
}


class LauncherClientError(RuntimeError):
    """客户端调用失败：守护侧错误信封或传输不可达——绝不夹带原始栈。"""


class LauncherClient:
    """:9421 守护 API 薄封装。

    request 缝（测试注入）：``request(method, url, body) -> (status, json)``。
    """

    def __init__(self, port: int = 9421, *, request=None):
        self.port = port
        self._request = request or self._http_request

    def alive(self) -> bool:
        try:
            self.status_all()
            return True
        except LauncherClientError:
            return False

    def status_all(self) -> dict:
        return self._call("GET", "/targets")

    def status_of(self, target: str) -> dict:
        return self._call("GET", "/targets/{0}".format(target))

    def start(self, target: str) -> dict:
        return self._call("POST", "/targets/{0}/start".format(target))

    def stop(self, target: str) -> dict:
        return self._call("POST", "/targets/{0}/stop".format(target))

    def restart(self, target: str) -> dict:
        return self._call("POST", "/targets/{0}/restart".format(target))

    def config_get(self) -> dict:
        return self._call("GET", "/config")

    def config_set(self, key: str, value) -> dict:
        return self._call("PUT", "/config", {key: value})

    def shutdown(self) -> dict:
        return self._call("POST", "/shutdown")

    @classmethod
    def log_path(cls, target: str) -> str:
        return _LOG_PATHS.get(target, "")

    def _call(self, method: str, path: str, body: dict | None = None):
        url = "http://127.0.0.1:{0}{1}".format(self.port, path)
        try:
            status, payload = self._request(method, url, body)
        except (urllib.error.URLError, OSError) as exc:
            raise LauncherClientError(
                "launcher daemon unreachable on :{0} ({1})".format(
                    self.port, type(exc).__name__
                )
            ) from exc
        if status >= 400:
            message = ""
            if isinstance(payload, dict):
                message = (payload.get("error") or {}).get("message", "")
            raise LauncherClientError(
                "daemon rejected {1} {2}: HTTP {0} {3}".format(
                    status, method, path, message
                )
            )
        return payload

    @staticmethod
    def _http_request(method: str, url: str, body: dict | None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = {}
            return exc.code, payload
