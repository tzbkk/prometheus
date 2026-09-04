"""launcher 侧 deepbackfill 网页扫码登录等待流（薄版）。

本模块零终端渲染：deepbackfill 服务（:9424）直出 PNG（/auth/qr.png）
与扫码页（/auth/page），launcher 只负责两件事：

1. ``start deepbackfill`` 后自动探测：轮询 /auth/status ≤8s——ok 立即返回
   （凭证已在且有效）；qr_pending/scanned → ``webbrowser.open``（标准库）
   打开扫码页（try/except + 返回值 False 双降级 = 纯 URL 打印兜底）→
   打印"浏览器出码，手机 QQ 扫码…" → 轮询至 ok/超时 → 成功提示（含 uin）
   回提示符。
2. ``auth`` 动词（薄版）：对运行中服务同流（开浏览器 + 等 ok）；服务未跑
   → 可读提示先 start deepbackfill（Shell 层判定，本模块不含监督面知识）。

等待中 Ctrl+C：安全回提示符——服务端 QR 会话保留（不投毒不停线程），
用户可随时打开 /auth/page 续扫。

I/O 缝：``echo`` / ``open_browser`` / ``urlopen`` / ``sleep`` / ``now``
由 Shell/测试注入——纯逻辑可离线单测。零新依赖（webbrowser 标准库）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

__all__ = [
    "BOOT_WINDOW_SEC",
    "POLL_SEC",
    "WAIT_TIMEOUT_SEC",
    "fetch_auth_status",
    "open_in_browser",
    "wait_for_web_login",
]

BOOT_WINDOW_SEC = 8.0     # start 后服务探测/QR 起会话的分辨窗
WAIT_TIMEOUT_SEC = 180.0  # 扫码等待总窗（QR ~2 分钟/码，服务端自动换码）
POLL_SEC = 0.5            # 本服务 /auth/status 轮询（非 ptqrlogin——无 1.5s 下限）


def _status_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/auth/status"


def _page_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/auth/page"


def fetch_auth_status(port: int, *, urlopen=None, timeout: float = 2.0) -> dict | None:
    """GET /auth/status → dict；连接拒/坏体 → None（服务仍在启动的常态）。"""
    urlopen = urlopen or urllib.request.urlopen
    try:
        with urlopen(_status_url(port), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def open_in_browser(url: str) -> bool:
    """webbrowser.open 双降级：异常或 False 一律 False（调用方打印 URL 兜底）。"""
    import webbrowser

    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 —— headless/无注册浏览器面：降级不炸
        return False


def wait_for_web_login(
    port: int,
    *,
    echo=print,
    open_browser=open_in_browser,
    urlopen=None,
    sleep=time.sleep,
    now=time.monotonic,
    boot_window: float = BOOT_WINDOW_SEC,
    timeout: float = WAIT_TIMEOUT_SEC,
    poll: float = POLL_SEC,
) -> dict | None:
    """三阶段等待流（详见模块 docstring）；成功返回 status dict。

    Ctrl+C → 可读取消消息（服务端 QR 会话保留）+ None——绝不炸 REPL。
    """
    page = _page_url(port)
    try:
        return _wait_stages(
            port, page, echo, open_browser, urlopen, sleep, now,
            boot_window, timeout, poll,
        )
    except KeyboardInterrupt:
        echo(f"\n已取消等待——服务端二维码会话保留，可随时打开 {page} 续扫")
        return None


def _wait_stages(port, page, echo, open_browser, urlopen, sleep, now,
                 boot_window, timeout, poll) -> dict | None:
    boot_deadline = now() + boot_window
    first = None
    while now() < boot_deadline:
        first = fetch_auth_status(port, urlopen=urlopen)
        if first is not None:
            break
        sleep(poll)
    if first is None:
        echo(f"deepbackfill 认证状态不可达（{_status_url(port)}）——服务可能仍在启动")
        return None
    if first.get("state") == "ok":
        echo(f"deepbackfill 凭证就绪（uin={first.get('uin', '?')}）")
        return first

    if open_browser(page):
        echo(f"已在浏览器打开扫码页：{page}")
    else:
        echo(f"无法自动打开浏览器——请手动访问：{page}")
    echo("浏览器出码，手机 QQ 扫码…（Ctrl+C 取消等待，二维码会话保留）")

    deadline = now() + timeout
    while now() < deadline:
        status = fetch_auth_status(port, urlopen=urlopen)
        if status is not None and status.get("state") == "ok":
            echo(
                f"登录成功（uin={status.get('uin', '?')}）—— "
                "全史回填已自动开跑（logs deepbackfill 看进度）"
            )
            return status
        sleep(poll)
    echo(f"等待扫码超时（{timeout:.0f}s）——服务端二维码会话保留，可随时打开 {page} 续扫")
    return None
