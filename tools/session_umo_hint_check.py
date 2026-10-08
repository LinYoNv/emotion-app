#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器判据：会话页把「umo 怎么用」讲清楚了吗。

## 情绪的问题（2026-09-27 截图）
「主动消息只能给到 default 这个默认用户 umo 吗，不能我自己新建会话
  然后去陪伴通过 umo 添加吗」

真相：新建会话**自动**分配独有 umo（`sess-<hex>`），机制本来就能用；
截图那条是**存量会话**（建在该功能之前）沿用 `default`，
而二者在列表里长得一样 ⇒ 用户以为"只能给 default"。

⇒ 修法不是加功能，而是**把这件事在界面上说出来**：
  ① 展开式说明「怎么让主动消息落到这个会话」三步
  ② 会话信息栏标出"这个 umo 被 N 条会话共用"（含 title 提示）

## 判据（每条都能失败；没有"跳过"）
  ① 会话页有 `.umo-howto` 折叠块，`summary` 提到"主动消息"与"default"
  ② 展开后三步都在：提到 `default`、`复制 umo`、`陪伴`
  ③ 切到一条 `default` 会话时，`#session-info` 出现 `warn` 类且文案含"共用"
  ④ 切到一条**专属 umo** 的会话时，`#session-info` **没有** `warn`，且显示它的 umo
  ⑤ 两种状态来回切都对（成对判据 —— 只验一边可能是"恒真"）
  ⑥ 手机视口（390×700）同样成立
  ⑦ 全程零 JS 异常

用法：python3 tools/session_umo_hint_check.py <base_url> <token>
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9255


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

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


#: 会话页状态 + 提示内容
PROBE = r"""
(() => {
  const info = document.querySelector('#session-info');
  const howto = document.querySelector('.umo-howto');
  const sum = howto ? (howto.querySelector('summary') || {}).textContent || '' : '';
  // 展开它（details 默认折叠，内容仍在 DOM —— 所以判据要按键查文本，
  // 不能靠"看不见就算没实现"）
  if (howto && !howto.open) howto.open = true;
  const howtoText = howto ? (howto.textContent || '') : '';
  const sel = document.querySelector('#session-select');
  const opts = sel ? [...sel.options].map(o => ({v: o.value, t: o.textContent})) : [];
  return JSON.stringify({
    vw: window.innerWidth,
    infoText: info ? info.textContent : null,
    infoWarn: info ? info.classList.contains('warn') : null,
    infoTitle: info ? (info.title || '') : '',
    hasHowto: !!howto,
    summaryText: sum.trim(),
    howtoText: howtoText.replace(/\s+/g, ' ').trim().slice(0, 500),
    howtoOpen: howto ? howto.open : null,
    options: opts,
    currentSession: (document.querySelector('#s-user-id') || {}).value || null,
    umoHint: (document.querySelector('#s-umo-hint') || {}).textContent || '',
    visibleTabs: [...document.querySelectorAll('.panel')]
      .filter(p => !p.classList.contains('hidden')).map(p => p.id),
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
        cdp.send("Page.navigate", url=base)
        time.sleep(2.2)
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

        def probe(wait: float = 2.0) -> dict:
            time.sleep(wait)
            cdp.send("Runtime.enable")
            return json.loads(cdp.eval(PROBE) or "{}")

        # ---- 进会话页 ----
        cdp.send("Page.navigate", url=base + "#/session")
        time.sleep(2.5)
        d = probe(1.5)
        print(f"  可见面板={d.get('visibleTabs')} 会话数={len(d.get('options') or [])}")

        # ① 折叠说明存在
        ck.add("① 会话页有「怎么让主动消息落到这个会话」折叠说明",
               bool(d.get("hasHowto")),
               f"summary={d.get('summaryText')!r}")
        ck.add("①b summary 提到了主动消息与 default",
               "主动消息" in str(d.get("summaryText"))
               and "default" in str(d.get("summaryText")),
               f"summary={d.get('summaryText')!r}")

        # ② 三步内容
        ht = str(d.get("howtoText") or "")
        for label, needle in (
            ("② 提到「不是 default」这条前提", "default"),
            ("②b 提到「复制 umo」", "复制 umo"),
            ("②c 提到去「陪伴」面板加用户", "陪伴"),
            ("②d 提到插件侧没用户会落到兜底", "回落"),
        ):
            ck.add(label, needle in ht, f"含 {needle!r}")

        # ③④⑤ 共用/专属两种状态都要验（成对 —— 只验一边可能是恒真）
        opts = d.get("options") or []
        # 切到第一条（列表顺序 = 会话页下拉顺序）
        states: dict[str, dict] = {}
        for o in opts[:6]:
            cdp.eval("(() => { const s = document.querySelector('#session-select');"
                     f" s.value = {json.dumps(o['v'])};"
                     " s.dispatchEvent(new Event('change')); })()")
            time.sleep(1.4)
            cdp.send("Runtime.enable")
            r = json.loads(cdp.eval(PROBE) or "{}")
            states[o["t"]] = r

        shared_states = {k: v for k, v in states.items() if v.get("infoWarn")}
        own_states = {k: v for k, v in states.items() if not v.get("infoWarn")}

        ck.add("③ 存在「非专属 umo」的会话，且信息栏确实标了 warn + 说明了原因",
               bool(shared_states)
               and all(
                   # 文案可能是「被 N 条会话共用」或「不是本条会话专属」——
                   # ⚠️ 第一版判据只找字面词「共用」，把正确文案判成红（断言错、产品对）。
                   ("共用" in str(v.get("infoText")) or "专属" in str(v.get("infoText")))
                   for v in shared_states.values()
               ),
               f"非专属会话={list(shared_states)} 例={next(iter(shared_states.values()), {}).get('infoText')!r}")
        ck.add("④ 存在「专属 umo」的会话，且信息栏**没有** warn、显示了它的 umo",
               bool(own_states)
               and all("共用" not in str(v.get("infoText")) for v in own_states.values())
               and all("sess-" in str(v.get("infoText")) for v in own_states.values()),
               f"专属会话={list(own_states)} 例={next(iter(own_states.values()), {}).get('infoText')!r}")
        ck.add("⑤ 两种状态真的不同（判据不是恒真）",
               bool(shared_states) and bool(own_states),
               f"共用 {len(shared_states)} 条 / 专属 {len(own_states)} 条")
        ck.add("⑤b warn 状态的 title 提示了动作",
               all("换一个专用 umo" in str(v.get("infoTitle")) for v in shared_states.values())
               if shared_states else False,
               f"例={next(iter(shared_states.values()), {}).get('infoTitle')!r}")

        # ⑥ 手机视口
        print("\n===== 手机 390×700 =====")
        cdp.send("Emulation.setDeviceMetricsOverride", width=390, height=700,
                 deviceScaleFactor=2, mobile=True)
        cdp.send("Page.reload", ignoreCache=True)
        time.sleep(2.2)
        d2 = probe(2.0)
        ck.add("⑥ 手机端折叠说明同样存在",
               bool(d2.get("hasHowto")), f"summary={d2.get('summaryText')!r}")
        ck.add("⑥b 手机端没有横向溢出（说明块没撑破布局）",
               cdp.eval("document.documentElement.scrollWidth <= window.innerWidth + 1") is True,
               f"scrollWidth={cdp.eval('document.documentElement.scrollWidth')} vw={d2.get('vw')}")

        ck.add("⑦ 全程零 JS 异常", len(cdp.exceptions) == 0, f"{cdp.exceptions[:2]}")

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
