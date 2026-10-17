#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用**真实 Chrome**（CDP）验「插件升级」这块界面。

🔴 为什么必须用真浏览器：
   `curl` 能证明"接口存在"，**证明不了"按钮在、点了会发请求、结果会显示"**。
   本项目踩过的原话：「前端改动必须用真浏览器打开看（`curl` 验不了 JS）」。

## 判据（**没有"跳过"** —— 前置不满足就是失败）

  ① 面板「插件」页里**真的有**升级区：`#plugin-update-proxy` + `#plugin-update-list`
  ② 升级列表**渲染出了插件行**，且每行都有版本号（用户判断"该不该升级"的唯一依据）
  ③ 每行**都有「升级」按钮**（不能只有文字）
  ④ 🔴 **点一下按钮真的发起了 `POST .../update`**（用 fetch 钩子抓，不看 DOM 猜）
  ⑤ 🔴 请求体里带了 `proxy` 字段（照真实现：代理是**每次请求传**的）
  ⑥ 取消确认框时**不发请求**（防止误点直接在用户机器上拉仓库）
  ⑦ 升级列表的插件集合 == 启停列表的插件集合（**同源**，不许少一个）

用法：
    .venv/bin/python tools/plugin_update_ui_check.py <base_url> <token>
退出码 0 = 全部通过；非 0 = 有判据失败（打印哪一条）。
"""
from __future__ import annotations

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
        self.ws = websocket.create_connection(ws_url, timeout=120)
        self._id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 失败: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str, *, await_promise: bool = False):
        res = self.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if res.get("exceptionDetails"):
            raise RuntimeError(f"JS 异常: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _launch() -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--remote-debugging-port={PORT}",
            # Chrome 111+ 默认拒绝带 Origin 的 CDP 连接（403）。不加这条会
            # `WebSocketBadStatusException: Handshake status 403 Forbidden`。
            "--remote-allow-origins=*",
            "--window-size=1280,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            _http_json("/json/version")
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Chrome 起不来")


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: plugin_update_ui_check.py <base_url> <token>")
        return 2
    base = sys.argv[1].rstrip("/") + "/"
    token = sys.argv[2]

    fails: list[str] = []
    ok: list[str] = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        (ok if cond else fails).append(label + (f" — {detail}" if detail else ""))

    proc = _launch()
    cdp = None
    try:
        tabs = _http_json("/json/list")
        page = next(t for t in tabs if t["type"] == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        # 登录（走真实登录接口拿 cookie，不手工塞头 —— 要验的正是浏览器路径）
        cdp.send("Page.navigate", {"url": base})
        time.sleep(2.5)
        login = cdp.eval(
            """
            (async () => {
              const r = await fetch('/api/v1/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({token: %s}),
                credentials: 'same-origin',
              });
              return r.status;
            })()
            """
            % json.dumps(token),
            await_promise=True,
        )
        check(login == 200, "登录接口 200", f"实际 {login}")

        cdp.send("Page.navigate", {"url": base})
        time.sleep(3.0)

        # ---- ① 升级区存在 ----
        has_ui = cdp.eval(
            """
            (() => {
              const proxy = document.getElementById('plugin-update-proxy');
              const list = document.getElementById('plugin-update-list');
              const res = document.getElementById('plugin-update-result');
              return {proxy: !!proxy, list: !!list, result: !!res};
            })()
            """
        )
        check(bool(has_ui and has_ui["proxy"]), "① 代理输入框 #plugin-update-proxy 存在")
        check(bool(has_ui and has_ui["list"]), "① 升级列表 #plugin-update-list 存在")
        check(bool(has_ui and has_ui["result"]), "① 结果提示区 #plugin-update-result 存在")

        # 切到插件页（升级区在那里）
        cdp.eval("location.hash = '#/plugins';")
        time.sleep(2.5)

        # 等列表渲染出来（异步取数，别用固定 sleep 赌）
        for _ in range(40):
            n = cdp.eval("document.querySelectorAll('#plugin-update-list .switch-row').length")
            if n and n > 0:
                break
            time.sleep(0.25)

        rows = cdp.eval(
            """
            (() => {
              const out = [];
              document.querySelectorAll('#plugin-update-list .switch-row').forEach(r => {
                const name = r.querySelector('.switch-label strong');
                const ver = r.querySelector('.switch-ver');
                const btn = r.querySelector('.plugin-update-btn');
                out.push({
                  name: name ? name.textContent : '',
                  ver: ver ? ver.textContent.trim() : '',
                  hasBtn: !!btn,
                  id: r.dataset.pluginId || '',
                });
              });
              return out;
            })()
            """
        ) or []

        # ---- ② 有行 + 有版本号 ----
        check(len(rows) > 0, "② 升级列表渲染出了插件行", f"实际 {len(rows)} 行")
        check(
            all(r["ver"] for r in rows),
            "② 每行都有版本号",
            f"缺版本号的行: {[r['name'] for r in rows if not r['ver']]}",
        )
        # ---- ③ 每行都有升级按钮 ----
        check(
            all(r["hasBtn"] for r in rows),
            "③ 每行都有「升级」按钮",
            f"缺按钮: {[r['name'] for r in rows if not r['hasBtn']]}",
        )

        # ---- ⑦ 与启停列表同源（集合相等）----
        # 🔴 两个列表用的**字段本来就不同**，不能直接比：
        #    * `#plugin-switch-list` 的 `data-module-path` = 停用名单里的原文
        #      （`astrbot_plugin_x.main`，带入口模块）；
        #    * `#plugin-update-list` 的 `data-plugin-id` = 接口里的 `id`
        #      （`astrbot_plugin_x`，不带 `.main`）。
        #    我第一版直接拿两个集合比 ⇒ 必然不等 ⇒ **断言错、产品对**。
        #    正确做法：两边都先取「插件标识」再比，即去掉 `.main` 后缀。
        switch_ids = cdp.eval(
            """
            (() => Array.from(document.querySelectorAll('#plugin-switch-list .switch-row'))
                      .map(r => r.dataset.modulePath || '').filter(Boolean)
                      .map(s => s.split('.')[0]).sort())()
            """
        ) or []
        update_ids = sorted(r["id"] for r in rows if r["id"])
        check(
            bool(switch_ids) and update_ids == switch_ids,
            "⑦ 升级列表与启停列表的插件集合一致（去掉 .main 后缀后逐条相等）",
            f"升级={update_ids} 启停={switch_ids}",
        )

        # ---- 装 fetch 钩子：记录 update 请求，并**拦住**真实网络 ----
        cdp.eval(
            """
            (() => {
              window.__updCalls = [];
              if (window.__updHooked) return;
              window.__updHooked = true;
              const orig = window.fetch;
              window.fetch = function(input, init) {
                const url = typeof input === 'string' ? input : (input && input.url) || '';
                if (url.includes('/update')) {
                  let body = null;
                  try { body = init && init.body ? JSON.parse(init.body) : null; } catch(e) {}
                  window.__updCalls.push({url, body, method: (init && init.method) || 'GET'});
                  // 🔴 不真打网络：返回一个假成功，避免判据真的去拉仓库
                  return Promise.resolve(new Response(
                    JSON.stringify({ok: true, message: '（判据拦截）', result: {}}),
                    {status: 200, headers: {'Content-Type': 'application/json'}}
                  ));
                }
                return orig.apply(this, arguments);
              };
            })()
            """
        )

        # ---- ⑥ 取消确认框 ⇒ 不发请求 ----
        cdp.eval("window.confirm = () => false;")
        cdp.eval(
            """
            (() => {
              const btn = document.querySelector('#plugin-update-list .plugin-update-btn');
              if (btn) btn.click();
            })()
            """
        )
        time.sleep(1.2)
        n_after_cancel = cdp.eval("(window.__updCalls || []).length") or 0
        check(n_after_cancel == 0, "⑥ 取消确认时不发请求", f"实际发了 {n_after_cancel} 次")

        # ---- ④⑤ 确认后真的发请求，且带 proxy ----
        cdp.eval("window.confirm = () => true;")
        cdp.eval(
            """
            (() => {
              const p = document.getElementById('plugin-update-proxy');
              if (p) p.value = 'http://127.0.0.1:9999';
              const btn = document.querySelector('#plugin-update-list .plugin-update-btn');
              if (btn) btn.click();
            })()
            """
        )
        time.sleep(2.0)
        calls = cdp.eval("window.__updCalls || []") or []
        check(len(calls) >= 1, "④ 点「升级」真的发起了请求", f"实际 {len(calls)} 次")
        if calls:
            c = calls[-1]
            check(str(c.get("method", "")).upper() == "POST", "④ 用的是 POST", c.get("method"))
            check("/update" in str(c.get("url", "")), "④ URL 含 /update", str(c.get("url")))
            body = c.get("body")
            check(isinstance(body, dict), "⑤ 请求体是 JSON 对象", repr(body)[:80])
            check(
                isinstance(body, dict) and "proxy" in body,
                "⑤ 请求体带 proxy 字段（照真实现：每次请求传）",
                repr(body)[:80],
            )
            check(
                isinstance(body, dict) and body.get("proxy") == "http://127.0.0.1:9999",
                "⑤ proxy 的值就是输入框里那个（没被改写）",
                repr(body)[:80],
            )

        # ---- 结果提示区有内容（用户看得见反馈）----
        res_text = cdp.eval(
            "(() => { const e = document.getElementById('plugin-update-result'); return e ? e.textContent.trim() : ''; })()"
        )
        check(bool(res_text), "结果提示区显示了反馈文字", repr(res_text)[:80])

    finally:
        if cdp:
            cdp.close()
        proc.kill()

    print("=" * 68)
    for line in ok:
        print(f"  ✅ {line}")
    for line in fails:
        print(f"  ❌ {line}")
    print("=" * 68)
    print(f"  通过 {len(ok)} · 失败 {len(fails)}")
    if fails:
        print("\n🔴 有判据失败（本项目不许跳过）")
        return 1
    print("\n✅ 插件升级 UI 判据全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
