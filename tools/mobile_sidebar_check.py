#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器判据：**手机视口下侧边栏能不能打开**。

## 情绪 2026-09-27 报障（截图 20:24）
「没有开关，打不开侧边栏」（手机 1260×2800 的页面，只有会话内容，没有任何导航入口）

## 怀疑根因（读代码得出，本脚本负责证实/推翻）
唯一的开关 `#sidebar-toggle` 在 `.sidebar-head` 里，
而手机端 `.sidebar` 是 `transform: translateX(-100%)` 的抽屉
⇒ **开关住在抽屉里，抽屉关着时开关跟着一起跑到屏幕外** ⇒ 用户永远打不开。

## 判据（每条都能失败；没有"跳过"）
  ① 390×700 视口下，`#sidebar-toggle` 的 rect **落在视口内**
     （x ≥ 0 且右边缘 ≤ 视口宽）—— 修复前必红
  ② 该按钮**能被点到**（`document.elementFromPoint` 命中的就是它或它的子节点）
     —— 「存在但被别的东西盖住」也要判红
  ③ **真点一下**（`Input.dispatchMouseEvent`，不是 `.click()`）
     → 侧边栏 `transform` 变成 `matrix(1,0,0,1,0,0)`（真的滑出来）
  ④ 滑出来后能看见导航项（`.nav-item` 至少 3 个在视口内）
  ⑤ 从抽屉里能**切到另一个 tab**（点「插件」→ `#tab-plugins` 可见）
  ⑥ 桌面视口（1280×900）行为不回归：开关在视口内、点击能收起/展开
  ⑦ 全程零 JS 异常

用法：python3 tools/mobile_sidebar_check.py <base_url> <token>
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9253


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

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.exceptionThrown":
                d = (msg.get("params") or {}).get("exceptionDetails") or {}
                self.exceptions.append(str(d.get("text") or d)[:250])
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

    def click_at(self, x: float, y: float) -> None:
        """真鼠标点击（`.click()` 会绕过"被遮挡"这类真实问题）。"""
        for t in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", type=t, x=x, y=y,
                      button="left", clickCount=1)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


PROBE = r"""
(() => {
  const app = document.querySelector('.app');
  const sb = document.querySelector('#sidebar');
  // 🔴 手机端该看的开关是**顶栏**那个（`#mobile-menu`）——
  //    抽屉里那个随抽屉移动，手机端打开抽屉前本来就点不到。
  //    桌面端反之（顶栏 display:none，只有侧边栏里那个能用）。
  const isMobile = window.matchMedia('(max-width: 640px)').matches;
  const tg = document.querySelector(isMobile ? '#mobile-menu' : '#sidebar-toggle');
  const r = (e) => { if (!e) return null; const b = e.getBoundingClientRect();
    return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width),
            h: Math.round(b.height), right: Math.round(b.right), bottom: Math.round(b.bottom)}; };
  const tgr = r(tg);
  let hit = null;
  if (tgr && tgr.w > 0) {
    const cx = tgr.x + tgr.w / 2, cy = tgr.y + tgr.h / 2;
    const el = document.elementFromPoint(cx, cy);
    hit = el ? (el.id || el.className || el.tagName) : null;
  }
  return JSON.stringify({
    vw: window.innerWidth, vh: window.innerHeight,
    appClass: app ? app.className : null,
    touched: isMobile ? '#mobile-menu' : '#sidebar-toggle',
    sidebarRect: r(sb),
    sidebarTransform: sb ? getComputedStyle(sb).transform : null,
    toggleRect: tgr,
    toggleDisplay: tg ? getComputedStyle(tg).display : null,
    hitAtToggleCenter: String(hit),
    clickable: !!(tgr && tgr.x >= 0 && tgr.right <= window.innerWidth
                  && tgr.y >= 0 && tgr.bottom <= window.innerHeight),
    navItemsInView: [...document.querySelectorAll('.nav-item')].filter((n) => {
      const b = n.getBoundingClientRect();
      return b.width > 0 && b.x >= 0 && b.right <= window.innerWidth;
    }).length,
    visibleTabs: [...document.querySelectorAll('.panel')]
      .filter(p => !p.classList.contains('hidden')).map(p => p.id),
    barTitle: (document.querySelector('#mobile-bar-title') || {}).textContent || '',
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
         "--remote-allow-origins=*", "--window-size=390,700", "about:blank"],
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
        cdp.send("Emulation.setDeviceMetricsOverride", width=390, height=700,
                 deviceScaleFactor=2, mobile=True)
        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)
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

        def probe(wait: float = 1.6) -> dict:
            time.sleep(wait)
            cdp.send("Runtime.enable")
            return json.loads(cdp.eval(PROBE) or "{}")

        # ================= 手机视口 =================
        print("\n===== 手机 390×700（情绪报障的场景）=====")
        cdp.send("Page.reload", ignoreCache=True)
        time.sleep(2.0)
        d = probe()
        print(f"  视口 {d.get('vw')}×{d.get('vh')} · app.class={d.get('appClass')!r}")

        tgr = d.get("toggleRect") or {}
        print(f"  触摸目标的开关 = {d.get('touched')}")
        ck.add("① 手机端有**顶栏里的**开关，且落在视口内",
               bool(d.get("clickable")) and d.get("touched") == "#mobile-menu",
               f"开关={d.get('touched')} rect={tgr} 侧边栏 transform={d.get('sidebarTransform')}")
        ck.add("①b 侧边栏关闭时，开关仍可见（开关不该住在抽屉里）",
               (tgr.get("w") or 0) > 0 and (tgr.get("x") or -999) >= 0,
               f"x={tgr.get('x')} w={tgr.get('w')}")
        ck.add("①c 顶栏显示了当前页标题（手机上看不见导航，得知道在哪页）",
               bool(str(d.get("barTitle") or "").strip()),
               f"标题={d.get('barTitle')!r}")
        ck.add("② 开关中心点能被点到（最顶层就是它**或它的子节点**）",
               "mobile-menu" in str(d.get("hitAtToggleCenter"))
               or "sidebar-toggle" in str(d.get("hitAtToggleCenter"))
               or "icon-btn" in str(d.get("hitAtToggleCenter"))
               # 命中 `burger` 是**正确**的：那是 button 内部画三条线的 span，
               # 点它冒泡到 button（`onclick` 绑在 button 上）。
               # ⚠️ 第一版判据只认 id，把它误判成红 —— 断言错、产品对。
               or "burger" in str(d.get("hitAtToggleCenter")),
               f"命中={d.get('hitAtToggleCenter')!r}")

        # ③ 真点一下（鼠标事件，不是 .click()）
        if tgr.get("w"):
            cx = tgr["x"] + tgr["w"] / 2
            cy = tgr["y"] + tgr["h"] / 2
            cdp.click_at(cx, cy)
            d2 = probe(1.8)
            print(f"  点击后 app.class={d2.get('appClass')!r} transform={d2.get('sidebarTransform')}")
            ck.add("③ 真点一下 → 抽屉真的滑出来（transform 归零）",
                   "drawer-open" in str(d2.get("appClass"))
                   and str(d2.get("sidebarTransform")) in ("matrix(1, 0, 0, 1, 0, 0)", "none"),
                   f"class={d2.get('appClass')} transform={d2.get('sidebarTransform')}")
            ck.add("④ 抽屉里有导航项可见（≥3 个在视口内）",
                   (d2.get("navItemsInView") or 0) >= 3,
                   f"可见导航项={d2.get('navItemsInView')}")

            # ⑤ 从抽屉切 tab —— 🔴 判据必须先**离开**插件页再切回来，
            #    否则"当前就在插件页"会让 `tab-plugins` 恒为可见（**假绿**，
            #    第一版就踩了这个：抽屉根本没开也报 ✅）。
            cdp.eval("(() => { const n = [...document.querySelectorAll('.nav-item')]"
                     ".find(a => (a.dataset.nav||'') === 'logs'); if (n) n.click(); })()")
            d3a = probe(1.8)
            left_ok = "tab-plugins" not in (d3a.get("visibleTabs") or [])
            # 抽屉应当已自动关上（applyRoute 里手机端 closeDrawer）
            ck.add("⑤-pre 已离开插件页（先切到日志页）",
                   left_ok, f"可见面板={d3a.get('visibleTabs')}")
            # 重新打开抽屉，再点「插件」
            cdp.eval("(() => { const m = document.querySelector('#mobile-menu'); if (m) m.click(); })()")
            d3b = probe(1.6)
            ck.add("⑤-pre2 顶栏汉堡能把抽屉再次打开",
                   "drawer-open" in str(d3b.get("appClass")),
                   f"class={d3b.get('appClass')}")
            cdp.eval("(() => { const n = [...document.querySelectorAll('.nav-item')]"
                     ".find(a => (a.dataset.nav||'') === 'plugins'); if (n) n.click(); })()")
            d3 = probe(2.0)
            ck.add("⑤ 从抽屉能切到「插件」页（抽屉关上、插件面板显示）",
                   "tab-plugins" in (d3.get("visibleTabs") or [])
                   and "drawer-open" not in str(d3.get("appClass")),
                   f"可见面板={d3.get('visibleTabs')} class={d3.get('appClass')}")
        else:
            ck.add("③ 真点一下 → 抽屉真的滑出来（transform 归零）", False, "开关没有可点区域")
            ck.add("④ 抽屉里有导航项可见（≥3 个在视口内）", False, "开关没有可点区域")
            ck.add("⑤ 从抽屉能切到「插件」页（抽屉关上、插件面板显示）", False, "开关没有可点区域")

        ck.add("⑦ 手机端零 JS 异常", len(cdp.exceptions) == 0, f"{cdp.exceptions[:2]}")

        # ================= 桌面视口（防回归）=================
        print("\n===== 桌面 1280×900（防回归）=====")
        cdp.send("Emulation.setDeviceMetricsOverride", width=1280, height=900,
                 deviceScaleFactor=1, mobile=False)
        cdp.send("Page.reload", ignoreCache=True)
        time.sleep(2.2)
        d4 = probe()
        t4 = d4.get("toggleRect") or {}
        ck.add("⑥ 桌面端开关仍在视口内", bool(d4.get("clickable")), f"rect={t4}")
        ck.add("⑥b 桌面端默认展开（侧边栏宽度 = 216 左右）",
               (d4.get("sidebarRect") or {}).get("w", 0) > 150,
               f"宽={(d4.get('sidebarRect') or {}).get('w')}")
        # 桌面点一下应能收起
        if t4.get("w"):
            cdp.click_at(t4["x"] + t4["w"] / 2, t4["y"] + t4["h"] / 2)
            d5 = probe(1.6)
            ck.add("⑥c 桌面端点一下能收起（宽度变小）",
                   (d5.get("sidebarRect") or {}).get("w", 999) < 100,
                   f"收起后宽={(d5.get('sidebarRect') or {}).get('w')} class={d5.get('appClass')}")
        else:
            ck.add("⑥c 桌面端点一下能收起（宽度变小）", False, "开关不可点")

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
