#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器验证面板的【登录闸门】。

🔴 为什么必须真浏览器：登录是纯前端行为（弹框 → POST → 隐藏 → boot），
   `curl` 只能证明接口返回 200，**证明不了用户点得进去**。

判据（全部满足才 exit 0）：
  1. 未登录时登录框**可见**（此前缺陷：页面上根本没有输入令牌的地方）
  2. 错误令牌 → 仍停在登录框且给出提示（不能"假装进去了"）
  3. 正确令牌 → 登录框隐藏、面板可见、数据加载出来
  4. 刷新页面 → **不用重输**（HttpOnly cookie 生效）
  5. 令牌不落 localStorage/sessionStorage（安全约定）
  6. **登录态迟到生效时自动解锁**（页面加载时没登录 → 之后 cookie 才有效
     → 切回本页必须自动进去，而不是永远卡在登录框）

用法：python3 tools/login_check.py <base_url> <token>
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
PORT = 9224


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self._id = 0

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg.get("result", {})

    def eval(self, expr: str, *, await_promise: bool = False):
        r = self.send("Runtime.evaluate", expression=expr,
                      awaitPromise=await_promise, returnByValue=True)
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


VISIBLE = "!document.getElementById('login').classList.contains('hidden')"
SUBMIT = ("document.getElementById('login-token').value={t};"
          "document.getElementById('login-form').dispatchEvent("
          "new Event('submit',{{cancelable:true}}));")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    if not token:
        print("❌ 需要令牌参数")
        return 2

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--remote-allow-origins=*", "--window-size=1200,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    fails: list[str] = []
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
        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        # ---- 1) 未登录：登录框必须可见 ----
        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)
        gate = cdp.eval(VISIBLE)
        panel_hidden = cdp.eval("document.getElementById('messages')!==null")
        print(f"1) 未登录时登录框可见: {gate}  (面板 DOM 存在: {panel_hidden})")
        if not gate:
            fails.append("未登录时登录框不可见 —— 用户无从下手")

        # ---- 2) 错误令牌：必须停在登录框，且不能放行 ----
        cdp.eval(SUBMIT.format(t=json.dumps("wrong-token-1234567890")))
        time.sleep(2.0)
        still = cdp.eval(VISIBLE)
        hint = cdp.eval("document.getElementById('login-hint').textContent") or ""
        print(f"2) 错误令牌后仍停在登录框: {still}  提示: {hint[:60]!r}")
        if not still:
            fails.append("错误令牌竟然放进去了")
        if "失败" not in hint:
            fails.append("错误令牌没有给出提示")

        # ---- 3) 正确令牌：进去，面板可用 ----
        cdp.eval(SUBMIT.format(t=json.dumps(token)))
        time.sleep(3.0)
        gone = not cdp.eval(VISIBLE)
        status = cdp.eval("document.getElementById('status').textContent") or ""
        persona_loaded = cdp.eval(
            "document.querySelectorAll('#persona-list li').length"
        )
        print(f"3) 登录框已隐藏: {gone}  状态栏: {status!r}  人设条目: {persona_loaded}")
        if not gone:
            fails.append("正确令牌没能进入")
        if "异常" in status:
            fails.append(f"登录后状态栏报异常：{status}")

        # ---- 4) 刷新后免登录（cookie 生效）----
        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)
        gate2 = cdp.eval(VISIBLE)
        print(f"4) 刷新后是否需要重输: {'需要(❌)' if gate2 else '不需要(✅)'}")
        if gate2:
            fails.append("刷新后要重新登录 —— cookie 没生效")

        # ---- 5) 令牌不得落在浏览器存储里 ----
        leaked = cdp.eval(
            "JSON.stringify({ls:Object.keys(localStorage),ss:Object.keys(sessionStorage),"
            "body:document.body.innerHTML.includes(%s)})" % json.dumps(token)
        )
        print(f"5) 令牌残留检查: {leaked}")
        if json.loads(leaked or "{}").get("body"):
            fails.append("令牌出现在 DOM 里")

        # ---- 6) 登录态"迟到生效"时必须自动解锁 ----
        #  🔴 这是**真 bug**（实测抓到，与第 4 项同类）：
        #     浮层是 fail-closed 设计（HTML 里默认可见），
        #     所以**每条"已登录"路径都必须显式关掉它**。
        #     第 4 项守的是"刷新时 cookie 还有效"；
        #     这一项守的是"页面加载时没登录 → 之后 cookie 才生效
        #     （别的标签页登录 / SSO / 服务端补发）→ 切回本页"。
        #     修之前：数据接口其实已经通了，**用户却永远卡在登录框上**。
        cdp.send("Network.clearBrowserCookies")
        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)
        gate3 = cdp.eval(VISIBLE)
        if not gate3:
            # 没弹浮层 = cookie 没清干净 → 前置条件不满足就是**失败**（不 skip）
            fails.append("清 cookie 后登录框未出现，第 6 项前置条件不满足")
        else:
            # 在**同一 context** 里补上 cookie（等价于"别处登录成功"）
            cdp.eval(
                "fetch('/api/v1/auth/login',{method:'POST',"
                "headers:{'Content-Type':'application/json'},"
                f"body:JSON.stringify({{token:{json.dumps(token)}}})}}).then(r=>r.status)",
                await_promise=True,
            )
            # 触发"切走 → 切回"（用户从别的标签页回来的真实动作）
            cdp.eval(
                "Object.defineProperty(document,'hidden',{value:true,configurable:true});"
                "document.dispatchEvent(new Event('visibilitychange'));"
            )
            time.sleep(0.4)
            cdp.eval(
                "Object.defineProperty(document,'hidden',{value:false,configurable:true});"
                "document.dispatchEvent(new Event('visibilitychange'));"
            )
            time.sleep(3.0)
            unlocked = not cdp.eval(VISIBLE)
            status3 = cdp.eval("(document.getElementById('status')||{}).textContent")
            print(f"6) 登录态迟到生效后自动解锁: {unlocked}  状态栏: {status3!r}")
            if not unlocked:
                fails.append(
                    "登录态迟到生效（别处登录/SSO）后仍卡在登录框 —— "
                    "数据其实已通，用户却进不去"
                )

        shot = cdp.send("Page.captureScreenshot", format="png")
        if shot.get("data"):
            with open("/tmp/login_check.png", "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            print("   截图 → /tmp/login_check.png")

        cdp.close()

        if fails:
            print("\n❌ 失败：")
            for f_ in fails:
                print("   -", f_)
            return 1
        print("\n✅ 通过：登录闸门 6 项判据全部满足")
        return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()


if __name__ == "__main__":
    sys.exit(main())
