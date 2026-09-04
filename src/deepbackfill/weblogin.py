"""ptlogin2 纯网页扫码登录客户端（路线 W——零部署）。

纯网页扫码登录（零部署）；**二维码在浏览器展示**——不做终端渲染，
服务直出 PNG，shell 自动开浏览器。

协议四步（活测参数，appid=1600001587/daid=823——pd.qq.com 专属对；
参考实现 fish2018/pansou plugin/qqpd/qqpd.go）：

1. ``GET https://xui.ptlogin2.qq.com/cgi-bin/xlogin?appid=1600001587&daid=823
   &style=8&hide_close_icon=1&s_url=https%3A%2F%2Fpd.qq.com%2Fexplore``
   → 种子 cookie（pt_login_sig）。
2. ``GET https://ssl.ptlogin2.qq.com/ptqrshow?appid=1600001587&e=2&l=M&s=3&d=72
   &v=4&t=<random>&daid=823&pt_3rd_aid=0``（Referer: xui.ptlogin2.qq.com）
   → PNG 原图 + Set-Cookie: qrsig。二维码寿命 ~2 分钟。
3. 轮询 ``GET https://ssl.ptlogin2.qq.com/ptqrlogin?u1=…&ptqrtoken=<hash33
   (qrsig,init=0)>&…&action=0-0-<epoch_ms>&…``（Cookie: qrsig）→ body
   ``ptuiCB('<code>','0','<check_url>','0','<msg>','<nickname>')``（参数
   6~8 个不定——宽解析）。码表：66 未扫 / 67 已扫待确认 / 65 过期（重跑
   ptqrshow 换新码）/ 0 成功（第 3 参 = check_sig URL）。节奏 ≥1.5s/次
   （归调用方 AuthSessionManager——本客户端单发不睡）。
4. ``GET <check_url>``（禁跟随重定向）→ Set-Cookie: p_skey / p_uin / uin
   （o0 前缀）落 .pd.qq.com 域。bkn = hash33(p_skey, init=5381)——与
   auth.bkn 同函数（保留资产，勿重复造）。

hash33：``h=init; for c in s: h += (h<<5)+ord(c); return h & 0x7FFFFFFF``。

transport 缝（最外层，学 tests/deepbackfill/test_auth.py 的 urlopen 缝
模式）：默认 = 禁重定向 opener（check_url 的 30x 必须停在某处读
Set-Cookie）；测试注入伪 transport。模块级 URL 常量在方法内运行期取值
（演练 sitecustomize 可整体重指 mock 服务）。零新依赖。
"""

from __future__ import annotations

import enum
import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode

__all__ = [
    "APPID",
    "DAID",
    "PTQRLOGIN_URL",
    "PTQRSHOW_URL",
    "QRState",
    "S_URL",
    "WebLoginClient",
    "WebLoginError",
    "XLOGIN_URL",
    "hash33",
]

logger = logging.getLogger(__name__)

APPID = "1600001587"
DAID = "823"
S_URL = "https://pd.qq.com/explore"

XLOGIN_URL = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
PTQRSHOW_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
PTQRLOGIN_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"

_REQUEST_TIMEOUT = 15.0


def hash33(s: str, init: int = 0) -> int:
    """ptlogin2 家族哈希（DJB 变体）：``h += (h<<5) + ord(c)`` 逐字节掩
    0x7FFFFFFF。init=0 → ptqrtoken（qrsig）；init=5381 → bkn（p_skey，
    与 auth.bkn 同函数）。"""
    h = init
    for c in s:
        h = (h + (h << 5) + ord(c)) & 0x7FFFFFFF
    return h


class WebLoginError(RuntimeError):
    """网页扫码登录失败（传输面/协议面/缺 p_skey）——可读错误，绝不夹带凭证。"""


class QRState(enum.Enum):
    """ptqrlogin 码表（§3）。"""

    WAITING = "waiting"    # 66 未扫
    SCANNED = "scanned"    # 67 已扫待手机确认
    EXPIRED = "expired"    # 65 过期——重跑 ptqrshow 换新码
    SUCCESS = "success"    # 0 成功——check_url 就位


_PTUI_CB_RE = re.compile(r"ptuiCB\s*\((.*)\)\s*;?\s*$", re.S)
_QUOTED_RE = re.compile(r"'([^']*)'")


@dataclass(frozen=True)
class PollResult:
    """poll_once 产物：状态 + 成功时的 check_url / 已扫时的 nickname。"""

    state: QRState
    check_url: str = ""
    nickname: str = ""
    code: int = -1
    message: str = ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """check_url 的 30x 必须就地停（Set-Cookie 在 302 响应上）——重定向
    处理器返回 None 时 urllib 抛 HTTPError(30x)，headers 可读。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_default_transport = urllib.request.build_opener(_NoRedirect).open


def _cookie_header(jar: dict[str, str], names: tuple[str, ...]) -> str:
    return "; ".join(f"{name}={jar[name]}" for name in names if name in jar)


def _merge_set_cookies(jar: dict[str, str], headers) -> None:
    """Set-Cookie 头并入 jar：``name=value; Path=…`` 取首段（值可含 =）。

    同名多域头（check_sig 一次回多根 ``p_skey``：真值 .pd.qq.com + 他域
    空串）非空恒胜——空值只补缺不覆写。
    """
    if headers is None:
        return
    raw_list = headers.get_all("Set-Cookie") or []
    for raw in raw_list:
        first = raw.split(";", 1)[0].strip()
        if not first or "=" not in first:
            continue
        name, _, value = first.partition("=")
        name, value = name.strip(), value.strip()
        if value or not jar.get(name):
            jar[name] = value


class WebLoginClient:
    """四步全封装的 QR 会话对象（单会话单实例；65 换码 = 再调 fetch_qr）。

    - ``fetch_qr() -> (png_bytes, qrsig)``：xlogin 种子（幂等）+ ptqrshow
      取 PNG 原图——浏览器直出面，零终端渲染。
    - ``poll_once(qrsig) -> PollResult``：单发 ptqrlogin（不睡——节奏归
      调用方，≥1.5s/次护栏在 AuthSessionManager）。
    - ``complete(check_url) -> {p_skey, p_uin, uin}``：禁跟随取
      Set-Cookie；uin 剥 o 前缀、p_uin 保 o 前缀（auth cookie 头规格）。
    """

    def __init__(self, *, transport=None, now_ms=None, timeout: float = _REQUEST_TIMEOUT):
        self._transport = transport or _default_transport
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._timeout = timeout
        self.cookies: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 传输面
    # ------------------------------------------------------------------
    def _get(self, url: str, *, headers: dict | None = None):
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        try:
            return self._transport(req, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            return exc  # 30x 就地停（_NoRedirect 面）；上层按需读 headers
        except (urllib.error.URLError, OSError) as exc:
            raise WebLoginError(f"ptlogin2 unreachable ({url.split('?')[0]}): {exc}") from exc

    # ------------------------------------------------------------------
    # 四步
    # ------------------------------------------------------------------
    def seed(self) -> str:
        """§1 xlogin → pt_login_sig（重复调用幂等刷新）。"""
        query = urlencode(
            {
                "appid": APPID,
                "daid": DAID,
                "style": "8",
                "hide_close_icon": "1",
                "s_url": S_URL,
            }
        )
        resp = self._get(f"{XLOGIN_URL}?{query}")
        _merge_set_cookies(self.cookies, resp.headers)
        return self.cookies.get("pt_login_sig", "")

    def fetch_qr(self) -> tuple[bytes, str]:
        """§2 ptqrshow → (PNG 原图字节, qrsig)。首次自动先 seed（§1）。"""
        if "pt_login_sig" not in self.cookies:
            self.seed()
        query = urlencode(
            {
                "appid": APPID,
                "e": "2",
                "l": "M",
                "s": "3",
                "d": "72",
                "v": "4",
                "t": repr(random.random()),
                "daid": DAID,
                "pt_3rd_aid": "0",
            }
        )
        resp = self._get(
            f"{PTQRSHOW_URL}?{query}",
            headers={"Referer": "https://xui.ptlogin2.qq.com/"},
        )
        with resp:
            png = resp.read()
        _merge_set_cookies(self.cookies, resp.headers)
        qrsig = self.cookies.get("qrsig", "")
        if not png or not qrsig:
            raise WebLoginError(
                f"ptqrshow returned no QR (png {len(png)} bytes, qrsig {'present' if qrsig else 'missing'})"
            )
        logger.info("ptqrshow: %d-byte PNG, qrsig acquired", len(png))
        return png, qrsig

    def poll_once(self, qrsig: str) -> PollResult:
        """§3 单发 ptqrlogin → 码表判定（宽正则：参数 6~8 个不定）。"""
        query = urlencode(
            {
                "u1": S_URL,
                "ptqrtoken": str(hash33(qrsig, 0)),
                "ptredirect": "1",
                "h": "1",
                "t": "1",
                "g": "1",
                "from_ui": "1",
                "ptlang": "2052",
                "action": f"0-0-{self._now_ms()}",
                "js_ver": "25100115",
                "js_type": "1",
                "login_sig": "",
                "pt_uistyle": "40",
                "aid": APPID,
                "daid": DAID,
            }
        )
        resp = self._get(
            f"{PTQRLOGIN_URL}?{query}",
            headers={
                "Referer": "https://xui.ptlogin2.qq.com/",
                "Cookie": _cookie_header(self.cookies, ("qrsig", "pt_login_sig")),
            },
        )
        with resp:
            body = resp.read().decode("utf-8", "replace").strip()
        match = _PTUI_CB_RE.match(body)
        if match is None:
            raise WebLoginError(f"ptqrlogin returned non-ptuiCB body: {body[:120]!r}")
        params = _QUOTED_RE.findall(match.group(1))
        if not params:
            raise WebLoginError(f"ptqrlogin body has no quoted params: {body[:120]!r}")
        try:
            code = int(params[0])
        except ValueError as exc:
            raise WebLoginError(f"ptqrlogin code not numeric: {params[0]!r}") from exc
        check_url = params[2] if len(params) > 2 else ""
        nickname = params[5] if len(params) > 5 else ""
        message = params[4] if len(params) > 4 else ""
        if code == 0:
            if not check_url:
                raise WebLoginError("ptqrlogin success without check_url (param 3 empty)")
            return PollResult(QRState.SUCCESS, check_url=check_url, code=code, message=message)
        if code == 67:
            return PollResult(QRState.SCANNED, nickname=nickname, code=code, message=message)
        if code == 65:
            return PollResult(QRState.EXPIRED, code=code, message=message)
        return PollResult(QRState.WAITING, code=code, message=message)

    def complete(self, check_url: str) -> dict[str, str]:
        """§4 GET check_url（禁跟随）→ {p_skey, p_uin, uin}（可读失败不泄值）。"""
        resp = self._get(check_url)
        _merge_set_cookies(self.cookies, resp.headers)
        p_skey = self.cookies.get("p_skey", "")
        if not p_skey:
            raise WebLoginError(
                "check_url did not set p_skey — login exchange failed (cookies seen: "
                + json.dumps(sorted(self.cookies))
                + ")"
            )
        uin_raw = (
            self.cookies.get("uin")
            or self.cookies.get("p_uin")
            or self.cookies.get("pt2gguin", "")
        )
        uin = uin_raw[1:] if uin_raw.startswith("o") else uin_raw
        if not uin:
            raise WebLoginError("check_url did not set uin — cannot build auth cookie")
        p_uin = self.cookies.get("p_uin") or f"o{uin}"
        if not p_uin.startswith("o"):
            p_uin = f"o{p_uin}"
        logger.info("web login complete: uin=%s p_skey=<masked>", uin)
        return {"p_skey": p_skey, "p_uin": p_uin, "uin": uin}
