#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真实 Chrome（CDP）测面板的图片渲染。

🔴 为什么必须用真浏览器：
   `curl` 只能验证"服务端返回了 images 字段"，
   **验证不了 JS 有没有把它渲染出来**。
   而实测踩到的坑恰恰在 JS 侧：流式更新用 `bubble.textContent = acc`
   会**清空所有子节点** → 图片被抹掉。

用法：python3 tools/browser_check.py <base_url> <token> [问题]
判据：退出码 0 = 图片节点真的出现且已加载（naturalWidth > 0）
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9223


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
    """极简 Chrome DevTools Protocol 客户端。"""

    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=120)
        self._id = 0

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg.get("result", {})

    def eval(self, expression: str, *, await_promise: bool = False):
        r = self.send(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=await_promise,
            returnByValue=True,
        )
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:16197/"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    prompt = sys.argv[3] if len(sys.argv) > 3 else "画一只戴墨镜的柯基"

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         # ⚠️ 新版 Chrome 默认拒绝跨源 WebSocket 握手，必须显式放行
         "--remote-allow-origins=*",
         "--window-size=1200,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        target = None
        for _ in range(40):
            time.sleep(0.25)
            try:
                target = next((t for t in _http_json("/json/list") if t["type"] == "page"), None)
                if target:
                    break
            except Exception:
                continue
        if not target:
            print("❌ Chrome 未就绪")
            return 1
        print("  ✅ Chrome 已连接")

        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)

        # 登录（写 cookie）
        status = cdp.eval(
            "fetch('/api/v1/auth/login',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            f"body:JSON.stringify({{token:{json.dumps(token)}}})}}).then(r=>r.status)",
            await_promise=True,
        )
        print(f"  登录: {status}")
        if status != 200:
            print("❌ 登录失败")
            return 1

        # 发消息触发生图
        cdp.eval(
            f"document.getElementById('input').value = {json.dumps(prompt)};"
            "document.getElementById('send').click();"
        )

        # 等图片节点出现
        found = 0
        for i in range(45):
            time.sleep(1)
            found = cdp.eval("document.querySelectorAll('.gen-image').length") or 0
            if found:
                print(f"  ✅ 第 {i + 1} 秒出现 {found} 个图片节点")
                break
        if not found:
            print("  ❌ 45 秒内未出现图片节点")

        # 等图片真正加载完（naturalWidth > 0 才算真渲染出来）
        loaded = 0
        for _ in range(30):
            loaded = cdp.eval(
                "[...document.querySelectorAll('.gen-image')]"
                ".filter(i=>i.naturalWidth>0).length"
            ) or 0
            if loaded:
                break
            time.sleep(0.5)

        # ⚠️ **再等正文**：图片事件先到，正文在**工具执行完的二次请求**之后才流回来。
        #    实测踩到：图片一出现就读 DOM → 正文还是空的，误判成"没有正文"。
        text = ""
        for _ in range(40):
            text = cdp.eval(
                "[...document.querySelectorAll('.msg.assistant .body')]"
                ".map(e=>e.textContent).join(' ').trim()"
            ) or ""
            if text:
                break
            time.sleep(0.5)

        state = cdp.eval(
            "JSON.stringify({"
            " imgCount: document.querySelectorAll('.gen-image').length,"
            " loaded: [...document.querySelectorAll('.gen-image')].filter(i=>i.naturalWidth>0).length,"
            " srcs: [...document.querySelectorAll('.gen-image')].map(i=>i.getAttribute('src')),"
            " text: [...document.querySelectorAll('.msg.assistant .body')].map(e=>e.textContent).join(' | ').slice(0,150)"
            "})"
        )
        print(f"  DOM: {state}")

        shot = cdp.send("Page.captureScreenshot", format="png")
        if shot.get("data"):
            with open("/tmp/panel.png", "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            print("  ✅ 截图 → /tmp/panel.png")

        cdp.close()

        # 判据：图**渲染且加载**，**且**有正文（只有图没文 = 体验不合格）
        ok_img = found > 0 and loaded > 0
        ok_text = bool(text.strip())
        print(f"\n  图: {'✅' if ok_img else '❌'}  正文: {'✅' if ok_text else '❌'}")
        if ok_img and ok_text:
            print("✅ 通过：图片已渲染加载，且有正文")
            return 0
        print("❌ 失败：图片或正文缺失")
        return 1
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()


if __name__ == "__main__":
    sys.exit(main())
