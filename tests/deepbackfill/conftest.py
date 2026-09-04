"""tests/deepbackfill 套件共享 fixture（纯网路线）。

- schema_assert / http：harness 薄胶水同形复制（tests/harness/conftest.py
  同形——服务 fixture 归各服务测试模块自持）。
- 伪 transport 族：FakeResponse / Headers / http_error /
  RecordingTransport——ptlogin2（weblogin）与 pd.qq.com auth 两侧的
  最外层缝替身（零网络；MD-100..102/116..118 消费）。
- 合成素材常量：与 tests/web_scraper/conftest.py 同形（GUILD/FEED_*/合成
  feed 工厂复用其构造惯例，跨 suite 不 import——域内自持纪律）。
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"

GUILD = "1000000000000001"
GUILD_NUMBER = "Takagi3channel"

UIN = 10001
NICKNAME = "archiver"
P_SKEY = "@XyZ123456"

QRSIG = "qr-sig-abcdef0123456789"
CHECK_URL = "https://ptlogin2.pd.qq.com/check_sig?pttype=1&uin=10001"

# 8x8 真灰度 PNG（73 字节，魔数齐全）——qr.png 端点/换码断言用
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAAAAADhZOFXAAAAEElEQVR4nGNg"
    "+I8GGcgSAQB8BB/hEhcEHgAAAABJRU5ErkJggg=="
)


class Headers:
    """Set-Cookie 可读头替身（``get_all(name)`` 形——email.Message 同族）。"""

    def __init__(self, set_cookies: list[str] | None = None):
        self._set_cookies = set_cookies or []

    def get_all(self, name, default=None):
        if name.lower() == "set-cookie":
            return list(self._set_cookies) or default
        return default


class FakeResponse:
    """urllib 响应替身：read() + 上下文管理器 + status + headers（transport 缝产物）。"""

    def __init__(self, body: bytes, status: int = 200, headers: Headers | None = None):
        self._buf = io.BytesIO(body)
        self.status = status
        self.headers = headers or Headers()

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def http_error(url: str, code: int, body: dict) -> urllib.error.HTTPError:
    """构造真 HTTPError（fp=BytesIO——exc.read() 可读裸 body）。"""
    return urllib.error.HTTPError(
        url, code, "err", {"Content-Type": "application/json"}, io.BytesIO(
            json.dumps(body).encode("utf-8")
        )
    )


def redirect_error(url: str, set_cookies: list[str]) -> urllib.error.HTTPError:
    """302 真 HTTPError + 多条 Set-Cookie（email.Message 头——真 urlopen 同形）。"""
    from email.message import Message

    headers = Message()
    for cookie in set_cookies:
        headers["Set-Cookie"] = cookie
    return urllib.error.HTTPError(url, 302, "Found", headers, io.BytesIO(b""))


class RecordingTransport:
    """伪 transport：按 (url 后缀) 路由到 canned 响应/异常，并记录请求。

    routes: {url_substr: response_like}——response_like 为 dict（200 JSON
    信封）/ FakeResponse / HTTPError 实例 / 任意 Exception 实例 / list（同
    键顺序出队——多轮场景）。未命中 → AssertionError。
    """

    def __init__(self, routes: dict):
        self.routes = {
            substr: (list(canned) if isinstance(canned, list) else canned)
            for substr, canned in routes.items()
        }
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url = req.full_url
        for substr, canned in self.routes.items():
            if substr in url:
                if isinstance(canned, list):
                    assert canned, f"no canned response left for {substr}"
                    canned = canned.pop(0)
                if isinstance(canned, BaseException):
                    raise canned
                if isinstance(canned, dict):
                    return FakeResponse(json.dumps(canned).encode("utf-8"))
                return canned
        raise AssertionError(f"unexpected transport URL: {url}")

    def last(self) -> urllib.request.Request:
        return self.requests[-1]

    def request_body(self, req) -> dict:
        return json.loads(req.data.decode("utf-8"))


class FakeSource:
    """AuthClient 凭据源替身：固定 creds + 可编程 remint（计次）。"""

    def __init__(self, creds, remint_creds=None, remint_raises=None):
        self.creds = creds
        self.remint_creds = remint_creds or creds
        self.remint_raises = remint_raises
        self.remint_calls = 0

    def credentials(self):
        return self.creds

    def remint(self):
        self.remint_calls += 1
        if self.remint_raises is not None:
            raise self.remint_raises
        self.creds = self.remint_creds
        return self.creds


class NoopSleep:
    """间隔缝替身：记录时长、零真实等待。"""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture(scope="session")
def schema_assert():
    from jsonschema import Draft202012Validator
    from zhizong.compile import compile_structure
    from zhizong.loader import load_corpus

    corpus = load_corpus(CONTRACTS_DIR)
    cache: dict[str, Draft202012Validator] = {}

    def _assert_matches(payload, structure_name):
        if structure_name not in cache:
            schema = compile_structure(corpus.structures()[structure_name], corpus)
            cache[structure_name] = Draft202012Validator(schema)
        errors = sorted(cache[structure_name].iter_errors(payload), key=lambda e: e.path)
        assert not errors, "{0} violations: {1}".format(
            structure_name, [e.message for e in errors]
        )

    return _assert_matches


@pytest.fixture
def http():
    import urllib.request

    def _request(method, url, payload=None, timeout=5):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read())

    def _parse(raw):
        text = raw.decode("utf-8")
        try:
            return json.loads(text)
        except ValueError:
            return text

    return _request
