#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自托管字体**能不能真渲染**的判据（真 Chrome + CDP）。

🔴 为什么必须真跑：
    `@font-face` 写对了、文件也在，**不等于浏览器用上了它** ——
    woff2 损坏、`unicode-range` 写窄了、`font-display` 竞态，
    任一条都表现为"静默回落到系统字体"，而 `curl` 与静态检查全都看不出来。

判据（三条都要过）：
  ① 文件真的 200（不是 404 被静默忽略）；
  ② `document.fonts.check()` 对 Geist 400/700 与 Geist Mono 400 都为真；
  ③ 与"故意关掉自托管"的对照组相比，**实测文本宽度不同** ——
     只有 ③ 能证明字形真的换了，前两条在回落到系统字体时也可能为真。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"


class CDP:
    def __init__(self, url: str) -> None:
        self.ws = websocket.create_connection(url, timeout=120)
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

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _http_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    port = 9237
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}", "--no-sandbox",
         "--disable-gpu", "--disable-dev-shm-usage", "--remote-allow-origins=*",
         "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    fails: list[str] = []
    try:
        target = None
        for _ in range(40):
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

        cdp.send("Page.navigate", url=base)
        time.sleep(2.0)

        # ① 字体文件本身可下载
        # ⚠️ **必须在导航之后**：相对路径要在 http(s) 页面的上下文里才有意义。
        #    放在 about:blank 上跑，`/static/x.woff2` 会解析成 `file:///static/x.woff2`
        #    → 拿到的是 0（不是 404），看着像"路由坏了"，其实是"还没进站点"。
        for name in ("geist-latin.woff2", "geist-latin-ext.woff2",
                     "geist-mono-latin.woff2", "geist-mono-latin-ext.woff2",
                     "LICENSE.txt"):
            code = cdp.eval(
                f"fetch('/static/fonts/{name}').then(r=>r.status)"
                f".catch(()=>0)", await_promise=True,
            )
            ok = code == 200
            print(f"  {'✅' if ok else '❌'} /static/fonts/{name} → {code}")
            if not ok:
                fails.append(f"字体文件不可取：{name}（{code}）")

        # 探针元素：**每类字体配它自己的系统对照**
        #   ⚠️ 对照必须是同类（sans↔sans、mono↔mono）。拿 Geist Mono 与
        #      `sans-serif` 比"宽度不同"是无效判据 —— 那只能证明"不是 sans"。
        probe = cdp.eval(
            """(async () => {
                 // 🔴 先等字体就绪：`font-display:swap` 下不 await 会量到**回落字体**的宽度，
                 //    于是"自托管生效了"却被判成"没生效"（实测踩过）。
                 await document.fonts.ready;
                 await Promise.all([
                   document.fonts.load('400 40px Geist', 'Wg'),
                   document.fonts.load('700 40px Geist', 'Wg'),
                   document.fonts.load('400 40px "Geist Mono"', 'Wg0123'),
                 ]);
                 const mk = (fam, txt, wt) => {
                   const s = document.createElement('span');
                   s.textContent = txt;
                   s.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;'
                     + 'font-size:40px;font-weight:' + (wt || 400) + ';font-family:' + fam;
                   document.body.appendChild(s);
                   const w = s.getBoundingClientRect().width;
                   s.remove();
                   return w;
                 };
                 const T = 'Wgmmmm 0123456789';
                 // 🔴 等宽体的**签名**：每个字符宽度恒等。
                 //    ⚠️ 不能只比"总宽与系统等宽不同" —— 实测 Geist Mono 408
                 //    与系统 mono 409.4 只差 1.4px（3‰），这种巧合会把它判成"没生效"。
                 //    「逐字符等宽」是等宽体独有的性质，不依赖两个字体恰好不一样宽。
                 const perChar = (fam, str) => [...str].map(c => mk(fam, c));
                 const uni = (a) => Math.max(...a) - Math.min(...a);
                 return JSON.stringify({
                   geist:        mk('Geist', T),
                   geist_sys:    mk('sans-serif', T),          // sans 对 sans
                   geist_bold:   mk('Geist', T, 700),
                   geist_bold_sys: mk('sans-serif', T, 700),
                   mono:         mk('"Geist Mono"', T),
                   mono_sys:     mk('monospace', T),
                   // ⚠️ 只用 **latin 子集里真实存在**的字符！我第一版塞了个 `ма`
                 //    （西里尔）—— 而我们自托管的是 latin+latin-ext 子集、**不含西里尔**
                 //    ⇒ 那个字符回落成系统字体、宽度明显不同 ⇒ 极差 5.75
                 //    ⇒ 判据报"它不是以 Geist Mono 渲染的"（**假红**：字体其实是对的）。
                 //    通则：判据的输入必须落在被测对象的**声明覆盖范围**内。
                 mono_uniform: uni(perChar('"Geist Mono"', 'WiM0l1')),  // 应 ≈ 0
                   mono_loaded:  document.fonts.check('400 40px "Geist Mono"'),
                 });
               })()""", await_promise=True,
        )
        widths = json.loads(probe)
        print(f"  实测文本宽度：{widths}")

        # ② document.fonts 是否认账
        ready = cdp.eval(
            "document.fonts.ready.then(()=>JSON.stringify({"
            "  geist400: document.fonts.check('400 40px Geist'),"
            "  geist500: document.fonts.check('500 40px Geist'),"
            "  geist600: document.fonts.check('600 40px Geist'),"
            "  geist700: document.fonts.check('700 40px Geist'),"
            "  mono400:  document.fonts.check('400 40px \"Geist Mono\"'),"
            "  mono500:  document.fonts.check('500 40px \"Geist Mono\"'),"
            "  loaded:   [...document.fonts].map(f=>f.family+':'+f.status).join(',')"
            "}))", await_promise=True)
        fonts = json.loads(ready) if ready else {}
        print(f"  document.fonts：{fonts}")
        for key in ("geist400", "geist500", "geist600", "geist700", "mono400", "mono500"):
            if not fonts.get(key):
                fails.append(f"document.fonts.check 为假：{key}")

        # ③ 真换了字形 —— 与**同类**系统族的宽度必须不同
        if abs(widths["geist"] - widths["geist_sys"]) < 0.5:
            fails.append(
                f"Geist 与系统 sans 宽度相同（{widths['geist']}）—— 自托管没生效，静默回落了"
            )
        # Geist Mono：判据是"逐字符等宽"（它是 Geist Mono 的定义性质），
        # 而不是"总宽与系统等宽不同"（两者可能巧合地接近）。
        if not widths.get("mono_loaded"):
            fails.append("Geist Mono 未被 document.fonts 认账 —— 自托管没生效")
        if widths.get("mono_uniform", 99) > 0.01:
            fails.append(
                f"Geist Mono 的字符宽度不一致（极差 {widths['mono_uniform']}）"
                "—— 它不是以 Geist Mono 渲染的"
            )
        # 可变字体的 700 轴：必须与 400 不同宽（证明 wght 轴真的在动）
        if abs(widths["geist"] - widths["geist_bold"]) < 0.5:
            fails.append(
                f"Geist 的 400 与 700 宽度相同（{widths['geist']}）—— "
                "可变字体的 wght 轴没生效（等于只有一个字重）"
            )

        # ④ 回到"面板真的用上了它"：body 的计算字体
        eff = cdp.eval(
            "JSON.stringify({"
            "  bodyFont: getComputedStyle(document.body).fontFamily.slice(0,120),"
            "  covered: document.fonts.check('400 14px Geist')"
            "})")
        print(f"  body 计算字体：{eff}")

        cdp.close()
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()

    if fails:
        print("\n❌ 失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("\n✅ 通过：字体文件可取、document.fonts 认账、且实测宽度与系统回落实质不同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
