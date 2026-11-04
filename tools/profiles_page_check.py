#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器判据：**配置文件分级**（2026-11-04）。

情绪：「astrbot 那边的设置是以"配置文件"进行分级，给会话选择用什么配置文件。合理了」

判据（全是"用户在界面上看得见"的）：
  ① 设置页有「配置文件」选择器
  ② 列表由后端驱动（至少含默认那份）
  ③ 切到另一份后，「会话」那一节的字段值跟着变（**分级真的生效**）
  ④ 默认那份的删除按钮**禁用**（删了没有回落目标）
  ⑤ 零 console 错误
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9271


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
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
        r = self.send("Runtime.evaluate", expression=expression,
                      awaitPromise=await_promise, returnByValue=True)
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191").rstrip("/")
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✅' if ok else '❌'} {name}{('  ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--remote-allow-origins=*", "--window-size=1400,1000", "about:blank"],
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

        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", cacheDisabled=True)
        cdp.eval("window.__errors=[];"
                 "window.addEventListener('error',e=>window.__errors.push(String(e.message)));"
                 "const oe=console.error;console.error=(...a)=>{window.__errors.push(a.join(' '));oe(...a)};")

        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)
        st = cdp.eval(
            "fetch('/api/v1/auth/login',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            f"body:JSON.stringify({{token:{json.dumps(token)}}})}}).then(r=>r.status)",
            await_promise=True,
        )
        check("登录成功", st == 200, f"HTTP {st}")
        if st != 200:
            return 1

        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)
        cdp.eval("location.hash='#/settings'")
        time.sleep(3.0)

        # ① 选择器存在
        exists = cdp.eval("!!document.getElementById('cfg-profile-picker')")
        check("① 配置文件选择器存在", bool(exists))

        # ② 列表由后端驱动
        opts = cdp.eval(
            "(() => {const s=document.getElementById('cfg-profile-picker');"
            "return s ? Array.from(s.options).map(o=>o.value) : [];})()"
        ) or []
        check("② 列表含默认那份", "default" in opts, str(opts))

        # ③ 切到另一份，字段值跟着变
        #    先造一份：直接走 API（判据不依赖 UI 的 prompt 交互）
        created = cdp.eval(
            "fetch('/api/v1/config-profiles',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({name:'判据用',clone_from:'default'})})"
            ".then(r=>r.json()).then(d=>d.profile.id)",
            await_promise=True,
        )
        if created:
            cdp.eval(
                "fetch('/api/v1/config-profile?profile=" + str(created) + "',{method:'PATCH',"
                "headers:{'Content-Type':'application/json'},"
                "body:JSON.stringify({session:{request_max_retries:9}})}).then(r=>r.status)",
                await_promise=True,
            )
            # 刷新设置页（重新读列表）
            cdp.eval("location.hash='#/chat'")
            time.sleep(0.8)
            cdp.eval("location.hash='#/settings'")
            time.sleep(3.0)
            got = cdp.eval(
                "(() => {const s=document.getElementById('cfg-profile-picker');"
                f"if(!s) return 'no-picker'; s.value={json.dumps(str(created))};"
                "s.dispatchEvent(new Event('change')); return s.value;})()"
            )
            time.sleep(2.5)
            val = cdp.eval(
                "(() => {const el=document.getElementById('cfg-session-request_max_retries');"
                "return el ? el.value : 'no-field';})()"
            )
            check("③ 切配置文件后字段值跟着变", str(val) == "9", f"读回 {val!r}")

        # ④ 默认那份的删除按钮禁用
        cdp.eval(
            "(() => {const s=document.getElementById('cfg-profile-picker');"
            "if(s){s.value='default';s.dispatchEvent(new Event('change'));}})()"
        )
        time.sleep(2.5)
        disabled = cdp.eval(
            "(() => {const b=document.getElementById('cfg-profile-delete');"
            "return b ? b.disabled : null;})()"
        )
        check("④ 默认那份的删除按钮禁用", disabled is True, f"disabled={disabled}")

        # ⑤ 零 console 错误
        errors = cdp.eval("(window.__errors||[]).slice(0,6)") or []
        check("⑤ 零 console 错误", not errors, str(errors)[:200])

        cdp.close()
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()

    print()
    if failures:
        print(f"❌ 失败 {len(failures)} 项：" + "、".join(failures))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
