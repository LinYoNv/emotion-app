#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器判据：插件拓展页**真的取到数了吗**（重点：「一起房间」）。

## 为什么需要它（情绪 2026-09-27 报的遗留项）
「一起房间」拓展页此前**永远取不到数**：它走 `window.AstrBotPluginPage` 桥，
但只生成 `page/x` **一种候选、且没有重试**
（`pages/一起房间/launcher.js:47`），而垫片从前不知道自己在哪个插件页里
⇒ 只能拼成 `/api/plugins/extensions/page/x` ⇒ **必然 404**。

其余插件（陪伴面板/记忆面板）自带 6 级候选重试，只是刷日志；
**只有「一起房间」是真坏**。

✅ 2026-09-27 的根因修复：服务端注入页面时把插件名写进
`<meta name="emotion-plugin-id">`，垫片据此把候选**补全**成
`<插件名>/page/x` → 一次命中。

## 判据（每条都能失败；没有"跳过"）
  ① 页面 `<head>` 里真的注入了插件身份 meta（且排在垫片脚本之前）
  ② 垫片 `window.AstrBotPluginPage` 存在
  ③ 🔴 **走垫片真发一次请求**（`page/status`）→ **不是 404**
     —— 这条是核心：修复前必红
  ④ 页面 JS 自己发起的请求里**没有 404**（用 CDP Network 域真监听，
     不是猜）
  ⑤ 页面渲染出了内容（不是白屏：body 有可见文本）
  ⑥ 全程零 JS 异常
  ⑦ 陪伴面板（有重试的那个）仍然正常 —— 别为了修一个弄坏另一个

用法：python3 tools/plugin_page_data_check.py <base_url> <token>
退出码 0 = 全过
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9251


class Check:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append((name, bool(ok), detail))
        print(f"  {'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))
        return bool(ok)

    def report(self) -> int:
        bad = [n for n, ok, _ in self.items if not ok]
        print(f"\n{'=' * 60}")
        print(f"{len(self.items) - len(bad)}/{len(self.items)} 通过")
        if bad:
            print("失败项：")
            for n in bad:
                print(f"  ❌ {n}")
            return 1
        print("✅ 全部通过")
        return 0


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=90)
        self._id = 0
        self.exceptions: list[str] = []
        self.requests: list[dict] = []
        self.responses: list[dict] = []

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            m = msg.get("method")
            if m == "Runtime.exceptionThrown":
                d = (msg.get("params") or {}).get("exceptionDetails") or {}
                self.exceptions.append(str(d.get("text") or d)[:250])
                continue
            if m == "Network.requestWillBeSent":
                p = msg.get("params") or {}
                self.requests.append({"url": (p.get("request") or {}).get("url", ""),
                                      "method": (p.get("request") or {}).get("method", "")})
                continue
            if m == "Network.responseReceived":
                p = msg.get("params") or {}
                r = p.get("response") or {}
                self.responses.append({"url": r.get("url", ""), "status": r.get("status", 0)})
                continue
            if msg.get("id") == self._id:
                return msg.get("result", {})

    def eval(self, expr: str, *, await_promise: bool = False):
        r = self.send("Runtime.evaluate", expression=expr,
                      awaitPromise=await_promise, returnByValue=True)
        res = r.get("result", {})
        if res.get("subtype") == "error":
            return {"__error__": res.get("description")}
        return res.get("value")

    def clear_net(self) -> None:
        self.requests.clear()
        self.responses.clear()

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


#: 页面健康度探针
PROBE = r"""
(() => {
  const meta = document.querySelector('meta[name="emotion-plugin-id"]');
  const head = document.head ? document.head.innerHTML : '';
  const bridgeIdx = head.indexOf('plugin_page_bridge.js');
  const metaIdx = head.indexOf('emotion-plugin-id');
  const body = document.body || document.createElement('body');
  const visText = (() => {
    const w = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    let n, out = '';
    while ((n = w.nextNode())) {
      const t = (n.textContent || '').trim();
      if (t) out += t + ' ';
    }
    return out.trim();
  })();
  return JSON.stringify({
    pluginId: meta ? meta.getAttribute('content') : null,
    metaBeforeBridge: (metaIdx >= 0 && bridgeIdx >= 0) ? (metaIdx < bridgeIdx) : null,
    hasBridge: typeof window.AstrBotPluginPage === 'object'
               && typeof window.AstrBotPluginPage.apiGet === 'function',
    bodyTextLen: visText.length,
    bodyTextHead: visText.slice(0, 160),
    title: document.title,
  });
})()
"""


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    ck = Check()

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--remote-allow-origins=*", "--window-size=1280,900", "about:blank"],
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

        # 先登录（拿 cookie），再直接导航到插件页
        cdp.send("Page.navigate", url=base)
        time.sleep(2.0)
        st = cdp.eval(
            "fetch('/api/v1/auth/login',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            f"body:JSON.stringify({{token:{json.dumps(token)}}})}}).then(r=>r.status)",
            await_promise=True,
        )
        print(f"登录: {st}")
        if st != 200:
            print("❌ 登录失败")
            return 1

        # ============ 主目标：「一起房间」 ============
        page_url = base.rstrip("/") + (
            "/api/v1/plugins/astrbot_plugin_together_companion/pages/"
            + urllib.parse.quote("一起房间") + "/"
        )
        print(f"\n===== 一起房间 =====\n{page_url}")
        cdp.clear_net()
        cdp.send("Page.navigate", url=page_url)
        time.sleep(5.0)
        cdp.send("Runtime.enable")
        d = json.loads(cdp.eval(PROBE) or "{}")

        ck.add("① head 里注入了插件身份 meta",
               d.get("pluginId") == "astrbot_plugin_together_companion",
               f"pluginId={d.get('pluginId')!r}")
        ck.add("①b meta 排在垫片脚本之前（顺序反了就读不到）",
               d.get("metaBeforeBridge") is True, f"metaBeforeBridge={d.get('metaBeforeBridge')}")
        ck.add("② 垫片 window.AstrBotPluginPage 存在", bool(d.get("hasBridge")))
        ck.add("⑤ 页面有可见内容（不是白屏）",
               (d.get("bodyTextLen") or 0) > 20,
               f"文本长度={d.get('bodyTextLen')} 开头={d.get('bodyTextHead')!r}")

        # ③ 🔴 核心：走垫片真发一次请求（修复前必 404）
        got = cdp.eval(
            "(async () => { const b = window.AstrBotPluginPage;"
            " if (!b) return JSON.stringify({error:'no bridge'});"
            " try { const r = await b.apiGet('page/status');"
            "   return JSON.stringify({ok:true, routeMissing: !!(r && (r.status==='error'"
            "      || /未找到.*路由|route.*not.*found/i.test(String(r.message||'')))),"
            "      keys: Object.keys(r||{}).slice(0,8)});"
            " } catch (e) { return JSON.stringify({ok:false, err:String(e.message||e)}); } })()",
            await_promise=True,
        )
        try:
            gotd = json.loads(got) if isinstance(got, str) else (got or {})
        except Exception:
            gotd = {"raw": got}
        ck.add("③ 🔴 走垫片请求 page/status 命中（不是「未找到该路由」）",
               bool(gotd.get("ok")) and not gotd.get("routeMissing"),
               f"{gotd}")

        # ④ 页面自己的请求里没有 404（CDP 真监听，不是猜）
        ext = [r for r in cdp.responses
               if "/api/plugins/extensions/" in r["url"] or "/api/plug/" in r["url"]]
        bad404 = [r for r in ext if r["status"] == 404]
        ck.add("④ 页面自动发起的插件 API 请求里没有 404",
               len(bad404) == 0,
               f"共 {len(ext)} 条，404 有 {len(bad404)} 条：{[r['url'].split('/api/')[1][:60] for r in bad404][:4]}")
        ck.add("④b 确实观察到了插件 API 请求（否则上面那条是空判据）",
               len(ext) > 0, f"{len(ext)} 条")

        ck.add("⑥ 全程零 JS 异常", len(cdp.exceptions) == 0,
               f"{cdp.exceptions[:2]}")

        # ============ 对照：陪伴面板（有重试的那个）仍正常 ============
        print("\n===== 对照：陪伴面板 =====")
        cdp.clear_net()
        cdp.send("Page.navigate", url=base.rstrip("/") + (
            "/api/v1/plugins/astrbot_plugin_private_companion/pages/companion-panel/"))
        time.sleep(6.0)
        cdp.send("Runtime.enable")
        d2 = json.loads(cdp.eval(PROBE) or "{}")
        resp2 = [r for r in cdp.responses if "/api/plugins/extensions/" in r["url"]]
        ok2 = [r for r in resp2 if r["status"] == 200]
        ck.add("⑦ 陪伴面板仍能取到数（200 的插件 API ≥1 条）",
               len(ok2) > 0, f"200 有 {len(ok2)} 条 / 共 {len(resp2)} 条")
        ck.add("⑦b 陪伴面板有可见内容",
               (d2.get("bodyTextLen") or 0) > 20, f"文本长度={d2.get('bodyTextLen')}")

        cdp.close()
        return ck.report()
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=8)
        except Exception:
            chrome.kill()


if __name__ == "__main__":
    sys.exit(main())
