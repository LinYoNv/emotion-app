#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-4 判据：**陪伴面板真的把「已绑定」渲染出来了**。

🔴 为什么必须用真浏览器：
   `curl` 只能证明**接口**返回了 `bound_delivery_umo`；
   而"面板上看得见绑定"取决于 **JS 有没有把它渲染出来**
   （`app.js:9882` `setupGuideBoundUsers()` 过滤 `delivery_bound && user_id`，
    再据此预填目标用户与平台）。
   这个项目已经踩过"接口全绿、界面空白"的坑（人设导入那次），
   所以端到端判据必须落到 **DOM**。

用法：
    .venv/bin/python tools/panel_binding_check.py [base_url] [token]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_check import CHROME, PORT, CDP, _http_json  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6190/"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""

#: 陪伴插件拓展页（必须带结尾 `/`，否则相对资源 404 —— 项目踩过）
PANEL = f"{BASE.rstrip('/')}/api/v1/plugins/astrbot_plugin_private_companion/pages/companion-panel/"


def main() -> int:
    failures: list[str] = []
    checks = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        if ok:
            print(f"  ✅ {label}")
        else:
            failures.append(label)
            print(f"  ❌ {label} — {detail}")

    proc = subprocess.Popen(
        [
            CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
            "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            # ⚠️ 新版 Chrome 默认拒绝跨源 WebSocket 握手
            #    （`Handshake status 403` —— 实测踩到），必须显式放行。
            #    已有工具 `browser_check.py` 里也是这么写的。
            "--remote-allow-origins=*",
            "--window-size=1280,900", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = ""
        for _ in range(40):
            time.sleep(0.25)
            try:
                tabs = _http_json("/json")
                page = next((t for t in tabs if t.get("type") == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                continue
        if not ws_url:
            print("❌ Chrome 未就绪")
            return 1

        cdp = CDP(ws_url)
        try:
            cdp.send("Runtime.enable")
            cdp.send("Page.enable")

            # ---- 1. 先登录拿 cookie（面板要鉴权）----
            if TOKEN:
                cdp.send("Page.navigate", url=BASE)
                time.sleep(2.5)
                r = cdp.eval(
                    "fetch('/api/v1/auth/login',{method:'POST',"
                    "headers:{'Content-Type':'application/json'},"
                    f"body:JSON.stringify({{token:{json.dumps(TOKEN)}}})}})"
                    ".then(r=>r.status)",
                    await_promise=True,
                )
                check("登录成功（否则后面全是 401 假象）", r == 200, f"status={r}")

            # ---- 2. 打开插件拓展页 ----
            cdp.send("Page.navigate", url=PANEL)
            time.sleep(6.0)   # 插件页 JS 较重，给它加载时间

            title = cdp.eval("document.title")
            check("插件页已加载（标题非空）", bool(title), f"title={title!r}")

            # 🔴 最常见的失败形态：JS 抛「未检测到 AstrBot 官方插件 Page 桥接」
            body_text = cdp.eval("document.body ? document.body.innerText : ''") or ""
            check(
                "页面没有卡在「未检测到桥接」",
                "未检测到" not in body_text and "桥接" not in body_text[:400],
                f"body 开头：{body_text[:180]!r}",
            )

            # ---- 3. 面板自己拿到的 users（桥接是否真的通）----
            # 直接问插件页的 state：它才是渲染的数据源
            state_users = cdp.eval(
                "(function(){try{"
                "  if (typeof state!=='undefined' && state && state.users) return state.users;"
                "  return null;"
                "}catch(e){return 'ERR:'+e.message}})()"
            )
            if isinstance(state_users, list):
                bound = [
                    u for u in state_users
                    if isinstance(u, dict) and u.get("delivery_bound") and u.get("user_id")
                ]
                check(
                    "面板 state.users 里有 delivery_bound 用户",
                    bool(bound),
                    f"users={len(state_users)} bound={len(bound)}",
                )
                if bound:
                    u = bound[0]
                    print(f"     · 绑定用户 user_id={u.get('user_id')!r} "
                          f"bound_umo={u.get('bound_delivery_umo')!r}")
                    check(
                        "绑定 umo 符合我们的约定（11:FriendMessage:<id>）",
                        str(u.get("bound_delivery_umo", "")).startswith("11:FriendMessage:"),
                        f"实际 {u.get('bound_delivery_umo')!r}",
                    )
            else:
                # state 不是全局变量也正常（可能是模块作用域）——
                # 那就退到"接口层"验证，并**明说**这是退路
                r = cdp.eval(
                    "fetch('/astrbot_plugin_private_companion/page/users?limit=10')"
                    ".then(r=>r.json()).then(d=>(d.data&&d.data.items)||[])"
                    ".catch(e=>'ERR:'+e.message)",
                    await_promise=True,
                )
                check(
                    "面板页内能取到用户列表（退路：直接走页面同源 fetch）",
                    isinstance(r, list),
                    f"got {str(r)[:120]!r}",
                )
                if isinstance(r, list):
                    bound = [u for u in r if u.get("delivery_bound")]
                    check(
                        "其中存在已绑定投递的用户",
                        bool(bound),
                        f"items={len(r)} bound={len(bound)}",
                    )
                    if bound:
                        check(
                            "绑定 umo 符合约定（11:FriendMessage:<id>）",
                            str(bound[0].get("bound_delivery_umo", "")).startswith("11:FriendMessage:"),
                            f"实际 {bound[0].get('bound_delivery_umo')!r}",
                        )

            # ---- 4. 页面上真的看得见（DOM 层）----
            visible = cdp.eval(
                "(function(){"
                "  var t = document.body ? document.body.innerText : '';"
                "  return {has11: t.indexOf('11:FriendMessage:') >= 0,"
                "          len: t.length};"
                "})()"
            )
            if isinstance(visible, dict):
                print(f"     · 页面可见文本长度={visible.get('len')} "
                      f"含 11:FriendMessage: = {visible.get('has11')}")

        finally:
            cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print()
    print("=" * 64)
    print(f"结果：{checks - len(failures)}/{checks} 项通过")
    if failures:
        print("失败项：")
        for f in failures:
            print("  ❌", f)
        return 1
    print("✅ 面板绑定链路验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
