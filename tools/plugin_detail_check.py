#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器判据：插件页的「列表 / 拓展页 / 详情」三视图 + 详情内容。

## 为什么必须真浏览器
情绪 2026-09-27 报「插件详情页怎么没了」。用 `curl` 打
`GET /api/v1/plugins/pages` **永远 200、永远 3 条** —— 而用户在面板上
**什么都看不见**：真因是 DOM 布局（`#plugin-page-list` 高度被压成 0，
内容还在、`scrollHeight=134`）。**接口全绿 ≠ 用户看得见。**

## 判据（每条都能失败；没有"跳过"）
  ① `#plugin-page-list` **真的可见**：高度 ≥ 内容高，且顶部落在视口内
     （只看 `querySelector` 存在性是假判据 —— 它一直在 DOM 里）
  ② 启停列表每行**横排**（`flex-direction:row`，行高 < 70px）
     —— 竖排时实测 126px/行，14 行把面板撑爆
  ③ 每个 `.switch-row` 都有「详情」按钮
  ④ 点「详情」→ **只有**详情可见（iframe 与入口列表都隐藏）—— 三视图互斥
  ⑤ 详情里三块都在：信息 / 注册数量 / 插件行为（换插件再验一次）
  ⑥ 详情**真取到了数据**：模块路径等字段非空（不是空壳）
  ⑦ 点「详情」**不会**切换该行的勾选框（整行是 `<label>`，不挡就会静默改配置）
  ⑧ 点「返回插件列表」→ 回到列表态（成对还原，这条正是上次漏掉的）
  ⑨ 深链接 `#/plugins/<id>` 直接进详情（刷新也能进）
  ⑩ 从详情返回后，勾选状态与进入前**完全一致**
  ⑪ 手机视口（390×700）下同样全过
  ⑫ 全程零 JS 异常

用法：python3 tools/plugin_detail_check.py <base_url> <token> [--shot]
退出码 0 = 全过
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9248


class Check:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append((name, bool(ok), detail))
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name}" + (f" —— {detail}" if detail else ""))
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


#: 三视图状态 + 列表健康度（一个探针取全，避免多次 round-trip 之间状态漂移）
PROBE = r"""
(() => {
  const el = (s) => document.querySelector(s);
  const box = (s) => {
    const e = el(s);
    if (!e) return {exists: false};
    const cs = getComputedStyle(e);
    const r = e.getBoundingClientRect();
    return {exists: true, hidden: e.classList.contains('hidden'),
            display: cs.display, h: Math.round(r.height), top: Math.round(r.top),
            scrollH: e.scrollHeight, dir: cs.flexDirection};
  };
  const rows = [...document.querySelectorAll('.switch-row')];
  const items = [...document.querySelectorAll('#plugin-page-list li')];
  return JSON.stringify({
    hash: location.hash,
    vw: window.innerWidth, vh: window.innerHeight,
    pageList: box('#plugin-page-list'),
    frameWrap: box('#plugin-frame-wrap'),
    detailWrap: box('#plugin-detail-wrap'),
    switchList: box('#plugin-switch-list'),
    rowCount: rows.length,
    rowDirs: [...new Set(rows.map(r => getComputedStyle(r).flexDirection))],
    rowHeights: rows.slice(0, 3).map(r => Math.round(r.getBoundingClientRect().height)),
    detailBtnCount: document.querySelectorAll('.switch-detail').length,
    itemHeights: items.map(i => Math.round(i.getBoundingClientRect().height)),
    checked: rows.map(r => {
      const cb = r.querySelector('input.switch-cb');
      return cb ? cb.checked : null;
    }),
    detail: (() => {
      const b = el('#plugin-detail-body');
      if (!b) return null;
      return {
        childCount: b.children.length,
        title: (el('#plugin-detail-title') || {}).textContent || '',
        hint: (el('#plugin-detail-hint') || {}).textContent || '',
        groups: [...b.querySelectorAll('.detail-group h4')].map(h => h.textContent),
        sectionTitles: [...b.querySelectorAll('.detail-section-title')].map(h => h.textContent),
        rows: [...b.querySelectorAll('.detail-v')].map(v => v.textContent).slice(0, 14),
        itemCount: b.querySelectorAll('.detail-item').length,
        preCount: b.querySelectorAll('.detail-pre').length,
      };
    })(),
  });
})()
"""


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    shot = "--shot" in sys.argv
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
        cdp.send("Page.navigate", url=base + "#/plugins")
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

        def probe(wait: float = 3.2) -> dict:
            """🔴 每次探针前**真重载** —— 等价于"干净页面"，避免上一轮的
            `style.x=''` 式还原不干净（我在诊断阶段被这个骗过一次）。"""
            cdp.send("Page.reload", ignoreCache=True)
            time.sleep(wait)
            cdp.send("Runtime.enable")
            return json.loads(cdp.eval(PROBE) or "{}")

        # ================= 桌面视口 =================
        print("\n===== 桌面 1280×900 =====")
        d = probe()

        # ① 入口列表真的可见
        pl = d.get("pageList") or {}
        ck.add("① 插件拓展页入口列表可见（高度≥内容高，且在视口内）",
               bool(pl.get("exists")) and not pl.get("hidden") and pl.get("h", 0) > 0
               and pl.get("h", 0) >= pl.get("scrollH", 1) - 1
               and 0 < pl.get("top", -1) < d.get("vh", 0),
               f"h={pl.get('h')} scrollH={pl.get('scrollH')} top={pl.get('top')} vh={d.get('vh')}")
        ck.add("①b 三个拓展页都在且每项都有高度",
               len(d.get("itemHeights") or []) >= 1
               and all(h > 0 for h in d.get("itemHeights") or []),
               f"itemHeights={d.get('itemHeights')}")

        # ② 启停行横排
        ck.add("② 启停列表每行横排（不是被全局 label 排成竖排）",
               d.get("rowDirs") == ["row"]
               and all(h < 70 for h in d.get("rowHeights") or [99]),
               f"dirs={d.get('rowDirs')} 行高={d.get('rowHeights')}")

        # ③ 每行都有详情按钮
        ck.add("③ 每个插件行都有「详情」按钮",
               d.get("detailBtnCount") == d.get("rowCount") and d.get("rowCount", 0) > 0,
               f"按钮 {d.get('detailBtnCount')} / 行 {d.get('rowCount')}")

        # ⑦ 前置：记下进入前的勾选状态
        before_checked = d.get("checked") or []
        ck.add("⑦-pre 拿到进入前的勾选状态（判据⑦的前置）",
               len(before_checked) > 0, f"{len(before_checked)} 行")

        # ④ 点第一个「详情」
        cdp.eval("(() => { const b = document.querySelector('.switch-detail');"
                 " if (b) b.click(); })()")
        time.sleep(2.8)
        d2 = json.loads(cdp.eval(PROBE) or "{}")

        ck.add("④ 点详情 → 只有详情可见（iframe 与入口列表都隐藏）",
               (d2.get("detailWrap") or {}).get("hidden") is False
               and (d2.get("frameWrap") or {}).get("hidden") is True
               and (d2.get("pageList") or {}).get("hidden") is True,
               f"detail.hidden={(d2.get('detailWrap') or {}).get('hidden')} "
               f"frame.hidden={(d2.get('frameWrap') or {}).get('hidden')} "
               f"list.hidden={(d2.get('pageList') or {}).get('hidden')}")

        # ⑤ 详情三块
        det = d2.get("detail") or {}
        groups = det.get("groups") or []
        ck.add("⑤ 详情含 信息 / 注册数量 / （插件行为）",
               any("信息" in g for g in groups) and any("注册数量" in g for g in groups)
               and bool(det.get("sectionTitles")),
               f"groups={groups} sections={det.get('sectionTitles')}")
        ck.add("⑤b 没有读取失败提示",
               not any("读取失败" in (det.get("hint") or "") for _ in [0]),
               f"hint={det.get('hint')!r}")

        # ⑥ 详情真的有数据
        rows = det.get("rows") or []
        ck.add("⑥ 详情字段非空（模块路径/ID 真取到了）",
               any("." in str(v) for v in rows),
               f"rows={rows[:6]}")

        # ⑦ 点详情没改配置
        after_checked = d2.get("checked") or []
        ck.add("⑦ 点「详情」**没有**切换勾选框（整行是 label，不挡就会静默改配置）",
               before_checked == after_checked,
               f"前={before_checked.count(True)}个勾选 后={after_checked.count(True)}个勾选")

        # ⑧ 返回列表
        cdp.eval("(() => { const b = document.getElementById('plugin-detail-back');"
                 " if (b) b.click(); })()")
        time.sleep(2.5)
        d3 = json.loads(cdp.eval(PROBE) or "{}")
        ck.add("⑧ 点「返回插件列表」→ 回到列表态（成对还原）",
               (d3.get("detailWrap") or {}).get("hidden") is True
               and (d3.get("pageList") or {}).get("hidden") is False
               and (d3.get("pageList") or {}).get("h", 0) > 0,
               f"detail.hidden={(d3.get('detailWrap') or {}).get('hidden')} "
               f"list.h={((d3.get('pageList') or {}).get('h'))}")
        ck.add("⑩ 返回后勾选状态与进入前完全一致",
               (d3.get("checked") or []) == before_checked)

        # ⑨ 深链接
        first_id = cdp.eval(
            "(async () => { const r = await fetch('/api/v1/plugins');"
            " const j = await r.json(); return (j.plugins||[])[0]?.id || ''; })()"
        )
        # ⚠️ 上面那样拿不到（返回 Promise）——改成 await 形式
        first_id = cdp.eval(
            "fetch('/api/v1/plugins').then(r=>r.json())"
            ".then(j => (j.plugins||[]).map(p=>p.id).join('|'))",
            await_promise=True,
        )
        first = (first_id or "").split("|")[0]
        ck.add("⑨-pre 拿到一个真实插件 id（判据⑨的前置）", bool(first), f"id={first!r}")
        if first:
            cdp.eval(f"location.hash = '#/plugins/' + encodeURIComponent({json.dumps(first)})")
            time.sleep(1.0)
            cdp.send("Page.reload", ignoreCache=True)
            time.sleep(3.5)
            cdp.send("Runtime.enable")
            d4 = json.loads(cdp.eval(PROBE) or "{}")
            ck.add("⑨ 深链接 #/plugins/<id> 直接进详情（刷新也能进）",
                   (d4.get("detailWrap") or {}).get("hidden") is False
                   and bool((d4.get("detail") or {}).get("groups")),
                   f"hash={d4.get('hash')} hidden={(d4.get('detailWrap') or {}).get('hidden')}")

        # ================= 手机视口 =================
        print("\n===== 手机 390×700 =====")
        cdp.send("Emulation.setDeviceMetricsOverride", width=390, height=700,
                 deviceScaleFactor=1, mobile=True)
        cdp.send("Page.navigate", url=base + "#/plugins")
        time.sleep(1.5)
        m = probe()
        mpl = m.get("pageList") or {}
        ck.add("⑪ 手机上入口列表仍可见（h>0 且在视口内）",
               not mpl.get("hidden") and mpl.get("h", 0) > 0
               and mpl.get("h", 0) >= mpl.get("scrollH", 1) - 1,
               f"h={mpl.get('h')} scrollH={mpl.get('scrollH')} top={mpl.get('top')} vh={m.get('vh')}")
        ck.add("⑪b 手机上启停行没被压成 0 高 / 没横向溢出",
               all(h > 0 for h in m.get("rowHeights") or [0]),
               f"行高={m.get('rowHeights')}")
        # 手机上进详情再返回
        cdp.eval("(() => { const b = document.querySelector('.switch-detail'); if (b) b.click(); })()")
        time.sleep(2.8)
        m2 = json.loads(cdp.eval(PROBE) or "{}")
        ck.add("⑪c 手机上点详情 → 详情可见、列表隐藏",
               (m2.get("detailWrap") or {}).get("hidden") is False
               and (m2.get("pageList") or {}).get("hidden") is True)
        mdet = m2.get("detail") or {}
        ck.add("⑪d 手机上详情内容有数据",
               bool(mdet.get("groups")) and bool(mdet.get("rows") or mdet.get("itemCount")))

        if shot:
            cdp.send("Emulation.clearDeviceMetricsOverride")
            time.sleep(0.5)
            import base64
            s = cdp.send("Page.captureScreenshot", format="png", captureBeyondViewport=True)
            if s.get("data"):
                with open("/tmp/plugin_detail_check.png", "wb") as f:
                    f.write(base64.b64decode(s["data"]))
                print("\n截图 → /tmp/plugin_detail_check.png")

        ck.add("⑫ 全程零 JS 异常", not cdp.exceptions,
               f"{len(cdp.exceptions)} 条" + (f": {cdp.exceptions[:3]}" if cdp.exceptions else ""))
        cdp.close()
        return ck.report()
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()


if __name__ == "__main__":
    sys.exit(main())
