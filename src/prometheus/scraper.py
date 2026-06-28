"""CDP-based feed scraper for the QQ Electron client.

Automates scrolling through guild feed pages and extracts posts in real-time,
bypassing the local database's sliding-window cache limitation.

Requires QQ to be launched with ``--remote-debugging-port`` (see ``cdp_port``
in ``prometheus.conf.json``). Disabled in current QQ builds — the inspector
is compiled out, so this module is kept for reference only.
"""

from __future__ import annotations

import json
import time
import urllib.request
import websocket

from . import config


def _cdp_get(path: str):
    host = config.get("cdp_host", "127.0.0.1")
    port = config.get("cdp_port", 9222)
    url = f"http://{host}:{port}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def list_targets() -> list[dict]:
    return _cdp_get("/json")


def find_feed_target() -> dict | None:
    """Find the renderer that is displaying guild feed content."""
    for t in list_targets():
        if t.get("type") != "page":
            continue
        title = t.get("title", "")
        url = t.get("url", "")
        if "guild" in url.lower() or "频道" in title or "guild" in title.lower():
            return t
    pages = [t for t in list_targets() if t.get("type") == "page"]
    return pages[0] if pages else None


class CDPSession:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=10)
        self._msg_id = 0

    def eval(self, expression: str, await_promise: bool = True, timeout: float = 30) -> dict:
        self._msg_id += 1
        self.ws.send(json.dumps({
            "id": self._msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "timeout": int(timeout * 1000),
            },
        }))
        deadline = time.time() + timeout + 5
        while time.time() < deadline:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == self._msg_id:
                return msg.get("result", {}).get("result", {})
        raise TimeoutError("CDP eval timed out")

    def eval_value(self, expression: str, **kw):
        result = self.eval(expression, **kw)
        return result.get("value")

    def close(self):
        self.ws.close()


def connect() -> CDPSession:
    target = find_feed_target()
    if not target:
        raise RuntimeError(
            "未找到 QQ 渲染页面。请确认 QQ 已用 --remote-debugging-port=9222 启动,"
            "且已打开频道页面。"
        )
    print(f"  连接目标: {target.get('title', '?')}  {target.get('url', '?')[:80]}")
    return CDPSession(target["webSocketDebuggerUrl"])


def inspect(cdp: CDPSession) -> None:
    """Dump useful DOM info for figuring out selectors."""
    info = cdp.eval_value(r"""
        (function(){
            const out = {title: document.title, url: location.href, scrollTargets: []};
            // 找所有可滚动容器
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (el.scrollHeight > el.clientHeight + 100 && el.clientHeight > 200) {
                    const rect = el.getBoundingClientRect();
                    out.scrollTargets.push({
                        tag: el.tagName,
                        cls: el.className?.toString?.()?.slice(0,80) || '',
                        id: el.id || '',
                        scrollH: el.scrollHeight,
                        clientH: el.clientHeight,
                        children: el.children.length,
                        x: Math.round(rect.x), y: Math.round(rect.y),
                        w: Math.round(rect.width), h: Math.round(rect.height),
                    });
                }
            }
            // 当前页面的文本摘要(前500字符)
            out.bodyText = document.body?.innerText?.slice(0, 500) || '';
            return out;
        })()
    """)
    print(f"\n  页面: {info.get('title')}  {info.get('url','')[:80]}")
    print(f"  可滚动容器: {len(info.get('scrollTargets', []))} 个")
    for st in info.get("scrollTargets", [])[:10]:
        print(f"    <{st['tag']} .{st['cls'][:40]}#{st['id']}> "
              f"scrollH={st['scrollH']} clientH={st['clientH']} "
              f"children={st['children']}  "
              f"pos=({st['x']},{st['y']}) size={st['w']}x{st['h']}")
    print(f"\n  页面文本前200字: {info.get('bodyText','')[:200]}")
