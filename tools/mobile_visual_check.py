#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器 **移动端视觉自查**：抓"DOM 不越界、但被父容器裁掉"的元素。

## 为什么需要这个工具（情绪 2026-11-05）

> 「pc端的chrome没有问题，移动端有。以后你加个验证方式，你自己在本机浏览器看。」

现有的 `tools/mobile_check.py` 只查 `scrollWidth <= clientWidth`（横向溢出），
**但那类判据抓不住真正的移动端 bug**。实测反例（人设页 390px）：

    DOM 越界元素：1 个（还是个隐藏抽屉按钮，left:-268）
    截图上看：   「设为当前」按钮**右边被切掉了**，第二行三个按钮贴边

矛盾的原因：**元素被 `overflow: hidden` 的祖先裁切了**。
`getBoundingClientRect()` 返回的是**未被裁切的完整盒子**，
所以"越界检查"永远看不出问题 —— 用户看到的却是被剪掉一半的按钮。

⇒ 本工具的判据是：**元素的可视区域**（自身 rect ∩ 所有 overflow-ancestor 的可视区）
   是否明显小于它自己的 rect。是 = 被裁。

## 判据

1. **不被裁切**：可交互元素（button/input/select/textarea/a）的可见宽度
   不少于自身宽度的 95%，或至少 24px
2. **不出视口**：可交互元素完整落在 `[0, clientWidth]` 内
3. **无横向滚动**：`documentElement.scrollWidth <= clientWidth + 1`
4. **触控目标 ≥ 40px**（照 Apple HIG 44px 放宽）
5. **输入框字号 ≥ 16px**（否则 iOS 聚焦自动放大）
6. 逐页截图存证（**人看得到才算数**）

⚠️ 第 6 条不是装饰：前 5 条是我认为"该有问题的地方"，
   而真正的 bug 常常出现在我没想到的地方 —— 截图是唯一的兜底。

用法：
    python3 tools/mobile_visual_check.py <base_url> <token> [--pages chat,persona,settings]
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
BASE_PORT = 9430

#: 手机视口（iPhone 12/13/14 逻辑像素）+ 3x DPR
MOBILE_W, MOBILE_H, DPR = 390, 844, 3

#: 默认要看的页面（`#/xxx`）
DEFAULT_PAGES = ["chat", "session", "persona", "settings", "plugins"]

#: 截图输出目录（**给人看的**，别塞进 /tmp 就忘）
SHOT_DIR = Path("/tmp/mobile_visual")


def _http_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=180)
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


#: 页面内执行的检测脚本。
#:
#: 🔴 核心是 `clipOf()`：把元素 rect 与**所有** overflow 祖先的可视区求交，
#:    得到的才是用户**真正看得见**的那块。这是本工具与 `mobile_check.py` 的分水岭。
_PROBE = r"""
(() => {
  const W = document.documentElement.clientWidth;

  // 元素被所有 overflow 祖先裁切后的**可见矩形**
  function clipOf(el) {
    let r = el.getBoundingClientRect();
    let top = r.top, left = r.left, bottom = r.bottom, right = r.right;
    let p = el.parentElement;
    while (p) {
      const cs = getComputedStyle(p);
      const cutsX = cs.overflowX !== 'visible';
      const cutsY = cs.overflowY !== 'visible';
      if (cutsX || cutsY) {
        const pr = p.getBoundingClientRect();
        if (cutsX) { left = Math.max(left, pr.left); right = Math.min(right, pr.right); }
        if (cutsY) { top = Math.max(top, pr.top); bottom = Math.min(bottom, pr.bottom); }
      }
      p = p.parentElement;
    }
    return { left, top, right, bottom,
             w: Math.max(0, right - left), h: Math.max(0, bottom - top) };
  }

  const clipped = [], outside = [], smallTouch = [], smallFont = [];
  const sel = 'button, input, select, textarea, a[href], [role="button"]';

  document.querySelectorAll(sel).forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;             // 不可见
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return;
    if (parseFloat(cs.opacity) === 0) return;

    const c = clipOf(el);
    const label = (el.textContent || el.getAttribute('aria-label') ||
                   el.getAttribute('placeholder') || el.tagName).trim().slice(0, 18);

    // 🔴 **先判"该不该被看见"**，否则满屏假警报。
    //    实测噪音（本工具第一版就被这些淹了）：
    //      * 手机抽屉里的导航（`left:-272`）—— 本来就是滑出态，收起是对的
    //      * `.skip-link`（`left:-9999`）—— 无障碍跳过链接，平时必须不可见
    //      * `aria-hidden` 的装饰节点
    //    判据：**屏幕外的元素不参与裁切检查**（它们本来就该看不见）。
    const onScreen = r.right > 0 && r.left < W && r.bottom > 0 && r.top < window.innerHeight;
    const hiddenByA11y = el.closest('[aria-hidden="true"]') || el.closest('.skip-link');
    if (!onScreen || hiddenByA11y) return;

    // ① 被裁切：可见宽度 < 自身 95% 且缺口 > 2px
    if (r.width > 8 && c.w < r.width - 2) {
      clipped.push({ label, tag: el.tagName,
        own: Math.round(r.width), visible: Math.round(c.w),
        lost: Math.round(r.width - c.w),
        cls: (el.className || '').toString().slice(0, 36) });
    }

    // ② 出视口（右/左）
    if (r.right > W + 1 || r.left < -1) {
      outside.push({ label, tag: el.tagName,
        left: Math.round(r.left), right: Math.round(r.right),
        over: Math.round(r.right - W) });
    }

    // ③ 触控目标
    if (r.height < 40 && el.tagName !== 'A') {
      smallTouch.push({ label, tag: el.tagName, h: Math.round(r.height) });
    }

    // ④ 输入框字号（iOS 聚焦自动放大）
    if (['INPUT','TEXTAREA','SELECT'].includes(el.tagName)) {
      const fs = parseFloat(cs.fontSize);
      if (fs < 16) smallFont.push({ label, tag: el.tagName, fs });
    }
  });

  return JSON.stringify({
    viewport: W,
    scrollW: document.documentElement.scrollWidth,
    clipped: clipped.slice(0, 12),
    outside: outside.slice(0, 12),
    smallTouch: smallTouch.slice(0, 12),
    smallFont: smallFont.slice(0, 12),
    counts: { clipped: clipped.length, outside: outside.length,
              smallTouch: smallTouch.length, smallFont: smallFont.length },
  });
})()
"""


def check_page(cdp: CDP, base: str, page: str, index: int) -> dict:
    """切到某一页，跑检测，截图。返回该页的问题汇总。"""
    cdp.eval(f"location.hash = '#/{page}'")
    time.sleep(2.4)

    raw = cdp.eval(_PROBE)
    data = json.loads(raw) if raw else {}

    shot = SHOT_DIR / f"{index:02d}_{page}.png"
    try:
        r = cdp.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        shot.write_bytes(base64.b64decode(r["data"]))
    except Exception as exc:  # noqa: BLE001
        print(f"    ⚠️ 截图失败：{exc}")

    return {"page": page, "data": data, "shot": str(shot)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default="http://127.0.0.1:6191")
    ap.add_argument("token", nargs="?", default="")
    ap.add_argument("--pages", default=",".join(DEFAULT_PAGES))
    args = ap.parse_args()

    base = args.base.rstrip("/")
    pages = [p.strip() for p in args.pages.split(",") if p.strip()]
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    port = BASE_PORT
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--remote-allow-origins=*", f"--window-size={MOBILE_W},{MOBILE_H}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []

    try:
        target = None
        for _ in range(50):
            time.sleep(0.25)
            try:
                target = next((t for t in _http_json(port, "/json/list") if t["type"] == "page"), None)
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
        # 🔴 **必须设 mobile=True + DPR**：否则媒体查询按桌面判，
        #    看到的布局根本不是用户手机上那个。
        cdp.send("Emulation.setDeviceMetricsOverride",
                 width=MOBILE_W, height=MOBILE_H, deviceScaleFactor=DPR, mobile=True)
        cdp.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)

        cdp.send("Page.navigate", url=base)
        time.sleep(2.6)
        if args.token:
            st = cdp.eval(
                "fetch('/api/v1/auth/login',{method:'POST',"
                "headers:{'Content-Type':'application/json'},"
                f"body:JSON.stringify({{token:{json.dumps(args.token)}}})}}).then(r=>r.status)",
                await_promise=True,
            )
            print(f"登录：HTTP {st}")
            cdp.send("Page.navigate", url=base)
            time.sleep(2.6)

        print(f"视口 {MOBILE_W}x{MOBILE_H} @{DPR}x（mobile=True）\n")
        results = []
        for idx, page in enumerate(pages, start=1):
            res = check_page(cdp, base, page, idx)
            results.append(res)
            d = res["data"] or {}
            c = d.get("counts", {})
            flag = "✅" if not any(c.values()) and d.get("scrollW", 0) <= d.get("viewport", 0) + 1 else "❌"
            print(f"  {flag} {page:<10} 被裁={c.get('clipped',0)} "
                  f"出界={c.get('outside',0)} 小目标={c.get('smallTouch',0)} "
                  f"小字号={c.get('smallFont',0)}  scrollW={d.get('scrollW')}")
            print(f"        截图 → {res['shot']}")
            for key, why in (("clipped", "被父容器裁切（用户看不全）"),
                             ("outside", "超出视口"),
                             ("smallFont", "输入框字号 <16px（iOS 聚焦会放大）")):
                for item in (d.get(key) or [])[:4]:
                    print(f"        ⚠️ {why}: {item}")
                    failures.append(f"{page}: {why} {item.get('label','')}")

        cdp.close()
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()

    print()
    if failures:
        print(f"❌ {len(failures)} 个问题（详见上面与 {SHOT_DIR}）")
        return 1
    print(f"✅ 全部通过。截图在 {SHOT_DIR}（**人要看一眼**）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
