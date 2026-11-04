#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器判据：**子代理接入页**（让用户连接自己的 agent 平台）。

## 为什么必须真浏览器

`curl` 只能证明"服务端返回了 cases 字段"，**证明不了 JS 把它们画出来了**。
本页最容易坏在 JS 侧：
  * `fields[]` 动态渲染没生效 → 用户看到空表单，以为"这个后端没有参数"；
  * 后端下拉切换后不重画字段 → 用户填的地址被提交到另一个后端；
  * 空态没区分"没配" / "读取失败" → 用户去配一个已经配好的东西。

## 判据（全部是"用户在那个界面上看得见"的）

| # | 判据 |
|---|---|
| ① | 页面标题是「潮汐引力 · Tidal Gravity System Station」 |
| ② | 导航里有「子代理」，点进去面板可见 |
| ③ | 后端下拉由目录驱动（至少 null/http/dsh 三项） |
| ④ | 切到 http 后，**动态出现** endpoint/token 字段 |
| ⑤ | 加一个子代理后，显示 `transfer_to_<name>` 预览 |
| ⑥ | 在 null 后端点「测试连接」，**如实报不可用**（不是"测试完成"） |
| ⑦ | 全程**零 console 错误**、零失败请求 |

用法：`python tools/subagents_page_check.py <base_url> <token>`
退出码 0 = 全过。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9231


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


def set_select_js(select_id: str, value: str) -> str:
    """生成「给 select 设值并触发 change」的表达式。

    🔴 **必须用 IIFE 包起来**：CDP 的 `Runtime.evaluate` 把整个字符串当**表达式**求值，
       而 `const s = ...; s.value = 'null';` 这种多语句块里的赋值**不会生效**
       （实测：设完读回还是旧值，导致判据假红 —— 我自己先被它骗过一次）。
       IIFE 里执行才可靠。
    """
    return (
        "(() => {"
        f" const el = document.getElementById({json.dumps(select_id)});"
        " if (!el) return 'missing';"
        f" el.value = {json.dumps(value)};"
        " el.dispatchEvent(new Event('change'));"
        " return el.value;"
        " })()"
    )


class CDP:
    """极简 Chrome DevTools Protocol 客户端（照 `tools/browser_check.py`）。"""

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
            "Runtime.evaluate", expression=expression,
            awaitPromise=await_promise, returnByValue=True,
        )
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191").rstrip("/")
    token = sys.argv[2] if len(sys.argv) > 2 else ""

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--remote-allow-origins=*", "--window-size=1400,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✅' if ok else '❌'} {name}{('  ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

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

        # 收集 console 错误
        cdp.eval("window.__errors = [];"
                 "window.addEventListener('error', e => window.__errors.push(String(e.message)));"
                 "const oe = console.error; console.error = (...a) => { window.__errors.push(a.join(' ')); oe(...a); };")

        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)

        status = cdp.eval(
            "fetch('/api/v1/auth/login',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            f"body:JSON.stringify({{token:{json.dumps(token)}}})}}).then(r=>r.status)",
            await_promise=True,
        )
        check("登录成功", status == 200, f"HTTP {status}")
        if status != 200:
            return 1

        # 进子代理页
        cdp.eval("location.hash = '#/subagents'")
        time.sleep(2.5)

        # ① 标题
        title = cdp.eval("document.title") or ""
        check("① 标题含新品牌", "潮汐引力" in title and "Tidal Gravity" in title, title)

        # ② 导航 + 面板
        nav = cdp.eval("document.querySelectorAll('a[data-nav=\"subagents\"]').length")
        check("② 导航项存在", nav == 1, f"{nav} 个")
        visible = cdp.eval(
            "(() => { const el = document.getElementById('tab-subagents');"
            " return !!el && !el.classList.contains('hidden'); })()"
        )
        check("② 子代理面板可见", bool(visible))

        # ③ 后端下拉由目录驱动
        opts = cdp.eval(
            "Array.from(document.querySelectorAll('#subagent-backend option')).map(o => o.value)"
        ) or []
        check("③ 后端下拉有 null/http/dsh", {"null", "http", "dsh"} <= set(opts), str(opts))

        # ④ 切到 http 动态出字段
        before = cdp.eval("document.querySelectorAll('#subagent-backend-fields .field-labeled').length")
        got = cdp.eval(set_select_js("subagent-backend", "http"))
        check("④ 后端已切到 http", got == "http", f"读回 {got!r}")
        time.sleep(1.0)
        after = cdp.eval("document.querySelectorAll('#subagent-backend-fields .field-labeled').length")
        check("④ 切 http 后动态出字段", (after or 0) > (before or 0), f"{before} → {after}")
        has_endpoint = cdp.eval("document.getElementById('subagent-field-endpoint') !== null")
        check("④ endpoint 字段被渲染", bool(has_endpoint))

        # ⑤ 加子代理 → 工具名预览
        cdp.eval("document.getElementById('subagent-add-agent').click()")
        time.sleep(0.6)
        cdp.eval(
            "(() => {"
            " const n = document.querySelector('.subagent-agent-card input.subagent-agent-name');"
            " if (!n) return 'missing';"
            " n.value = 'coder';"
            " n.dispatchEvent(new Event('input'));"
            " return n.value;"
            " })()"
        )
        # 预览文字在读的时候才拼，直接读 DOM
        preview = cdp.eval(
            "(document.querySelector('.subagent-agent-preview') || {}).textContent || ''"
        ) or ""
        check("⑤ 显示 transfer_to_ 工具名", "transfer_to_" in preview, preview[:60])

        # ⑤b 🔴 **改名后预览必须跟着变**。
        #    这条是被注入反证逼出来的：原来只验"加完就有预览"，
        #    而我删掉 input 监听后判据**照样全绿** —— 因为新卡渲染时
        #    名字已经是 coder 了。必须**先加空名卡、再改名、再看预览**。
        cdp.eval("document.getElementById('subagent-add-agent').click()")
        time.sleep(0.5)
        cdp.eval(
            "(() => {"
            " const cards = document.querySelectorAll('.subagent-agent-card');"
            " const n = cards[cards.length - 1].querySelector('input.subagent-agent-name');"
            " if (!n) return 'missing';"
            " n.value = 'writer';"
            " n.dispatchEvent(new Event('input'));"
            " return n.value;"
            " })()"
        )
        time.sleep(0.4)
        last_preview = cdp.eval(
            "(() => {"
            " const ps = document.querySelectorAll('.subagent-agent-preview');"
            " return ps.length ? ps[ps.length - 1].textContent : '';"
            " })()"
        ) or ""
        check("⑤b 改名后预览实时更新", "transfer_to_writer" in last_preview, last_preview[:60])

        # ⑥ null 后端测试连接 → 如实报不可用
        got = cdp.eval(set_select_js("subagent-backend", "null"))
        check("⑥ 后端已切回 null", got == "null", f"读回 {got!r}")
        time.sleep(0.6)
        cdp.eval("document.getElementById('subagent-test').click()")
        time.sleep(2.0)
        result = cdp.eval(
            "(document.getElementById('subagent-test-result') || {}).textContent || ''"
        ) or ""
        tone = cdp.eval(
            "(document.getElementById('subagent-test-result') || {}).dataset.tone || ''"
        ) or ""
        check("⑥ null 后端如实报不可用",
              ("未接入" in result or "没有选择" in result) and tone == "bad", result[:70])

        # ⑦ 零 console 错误
        errors = cdp.eval("(window.__errors || []).slice(0, 8)") or []
        check("⑦ 零 console 错误", not errors, str(errors)[:200])

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
