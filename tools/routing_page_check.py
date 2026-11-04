#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器判据：**设置页只有配置文件；会话页切换配置文件**（2026-11-04）。

情绪：「设置界面就只有配置文件，不按会话。然后"会话"界面切换配置文件。」
      「要么你就学 astrbot，在配置器页面按 umo/会话选配置文件」

判据：
  ① 设置页**没有**"选会话"控件（那是会话页的事）
  ② 设置页有配置文件编辑区（`#cfg-session-fields` 有内容）
  ③ 会话页每行有「用哪份配置文件」下拉
  ④ 选一个新配置文件 → 真的写进 umo 路由表（读回 API 验证）
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
PORT = 9281


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
         "--remote-allow-origins=*", "--window-size=1400,1200", "about:blank"],
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

        # 造两个配置文件（判据用，事后由调用方清理）
        # ⚠️ 名字要**唯一**：重复跑会撞「已经有叫 X 的配置文件」⇒ 400。
        #    我第一次用固定名字，第二次跑就建不出来（判据自己把自己绊倒）。
        a = cdp.eval(
            "fetch('/api/v1/config-profiles',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({name:'判据配置-'+Date.now(),clone_from:'default'})})"
            ".then(async r=>{const d=await r.json();"
            "return r.ok && d.profile ? d.profile.id : ('ERR '+r.status+' '+JSON.stringify(d));})",
            await_promise=True,
        )

        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)
        cdp.eval("location.hash='#/settings'")
        time.sleep(3.0)

        # ① 设置页没有"选会话"
        picker = cdp.eval("!!document.getElementById('cfg-session-picker')")
        check("① 设置页没有「选会话」控件", not picker)

        # ② 设置页有配置文件编辑区且有字段
        fields = cdp.eval(
            "document.querySelectorAll('#cfg-session-fields .cfg-field').length"
        ) or 0
        check("② 设置页有配置文件字段", fields > 0, f"{fields} 个")

        # ③ 会话页每行有配置文件下拉
        # ⚠️ 必须**重新进一次**会话页：`#/session` 的 loader 才会把
        #    刚建的配置文件拉进 `state.cfgProfiles`。不重进的话下拉里没有它，
        #    `select.value = <新id>` 会**静默变成空串**（我第一次就是这么假绿的）。
        cdp.eval("location.hash='#/chat'")
        time.sleep(0.8)
        cdp.eval("location.hash='#/session'")
        time.sleep(2.5)
        picks = cdp.eval("document.querySelectorAll('.sess-profile-select').length") or 0
        check("③ 会话页有配置文件下拉", picks > 0, f"{picks} 个")

        # ④ 选一个 → 真的写进路由表
        # ⚠️ **不许"建不出配置就跳过"** —— 跳过判据 = 假通过（本项目铁律）。
        check("④a 判据用的配置文件建出来了", bool(a), repr(a))
        if picks and a:
            cdp.eval(
                "(() => {const s=document.querySelector('.sess-profile-select');"
                f"if(!s) return 'no'; s.value={json.dumps(str(a))};"
                "s.dispatchEvent(new Event('change')); return s.value;})()"
            )
            time.sleep(2.0)
            routes = cdp.eval(
                "fetch('/api/v1/config-routing').then(r=>r.json()).then(d=>JSON.stringify(d.routes))",
                await_promise=True,
            )
            check("④b 选择写进了 umo 路由表", str(a) in str(routes), str(routes)[:120])
        else:
            check("④b 选择写进了 umo 路由表", False, "前置不满足（没有下拉或没建出配置）")

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
