#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器验证**手机端可用性**。

🔴 为什么必须真浏览器 + 真手机视口：
   媒体查询是否生效、元素有没有横向溢出、触控目标够不够大 ——
   `curl` 全都验不了，改 CSS 后"看起来应该没问题"是**最容易翻车**的地方。

判据（全部满足才 exit 0）：
  1. **无横向溢出**（`scrollWidth <= clientWidth + 1`）—— 手机上左右滑动是最烦的
  2. **触控目标 ≥ 40px**（按钮高度），照 Apple HIG 的 44px 放宽一点
  3. **人设页单列**（`.split` 的 aside 与 editor **上下堆叠**，不是左右分栏）
  4. **片段表转卡片**（`thead` 隐藏，`td::before` 显示字段名）
  5. **输入框字号 ≥16px** —— 否则 iOS 聚焦时会自动放大页面
  6. 截图存证

用法：python3 tools/mobile_check.py <base_url> <token>
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9226

#: 模拟 iPhone 12/13 的逻辑视口（CSS 像素）
MOBILE_W, MOBILE_H = 390, 844


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=90)
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

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    if not token:
        print("❌ 需要令牌参数")
        return 2

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--remote-allow-origins=*",
         f"--window-size={MOBILE_W},{MOBILE_H}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    fails: list[str] = []
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
        # 关键：用设备指标模拟**真手机**（含 DPR 与触控），不是只把窗口改窄
        cdp.send("Emulation.setDeviceMetricsOverride",
                 width=MOBILE_W, height=MOBILE_H,
                 deviceScaleFactor=3, mobile=True)
        cdp.send("Emulation.setTouchEmulationEnabled", enabled=True)

        cdp.send("Page.navigate", url=base)
        time.sleep(2.5)

        # 登录
        st = cdp.eval(
            "fetch('/api/v1/auth/login',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            f"body:JSON.stringify({{token:{json.dumps(token)}}})}}).then(r=>r.status)",
            await_promise=True,
        )
        if st != 200:
            print(f"❌ 登录失败：{st}")
            return 1
        cdp.send("Page.navigate", url=base)
        time.sleep(2.0)

        vw = cdp.eval("document.documentElement.clientWidth")
        print(f"视口宽度: {vw}px（模拟 {MOBILE_W}x{MOBILE_H} @3x）")

        # ---- 1) 横向溢出 ----
        over = cdp.eval(
            "JSON.stringify({"
            " doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,"
            " bad: [...document.querySelectorAll('*')]"
            "   .filter(e => e.scrollWidth > e.clientWidth + 1 && e.clientWidth > 0)"
            "   .slice(0,8).map(e => (e.id||e.className||e.tagName)+':'+e.scrollWidth+'>'+e.clientWidth)"
            "})"
        )
        d = json.loads(over or "{}")
        print(f"1) 横向溢出: 文档 {d.get('doc')}px  溢出的元素 {d.get('bad')}")
        if (d.get("doc") or 0) > 1:
            fails.append(f"页面横向溢出 {d['doc']}px（手机上会左右晃）")
        if d.get("bad"):
            fails.append(f"元素横向溢出：{d['bad']}")

        # ---- 2) 触控目标大小 ----
        small = cdp.eval(
            "JSON.stringify([...document.querySelectorAll('button')]"
            " .map(b => ({t:(b.textContent||'').trim().slice(0,10), h:Math.round(b.getBoundingClientRect().height)}))"
            " .filter(x => x.h > 0 && x.h < 40))"
        )
        tiny = json.loads(small or "[]")
        print(f"2) 小于 40px 的按钮: {tiny}")
        if tiny:
            fails.append(f"触控目标过小：{tiny}")

        # ---- 3) 人设页单列（切到人设页） ----
        cdp.eval("document.querySelector('.tab[data-tab=\"persona\"]').click()")
        time.sleep(1.2)
        layout = cdp.eval(
            "(() => { const s=document.querySelector('#tab-persona .split');"
            " const a=s.querySelector('aside'), e=s.querySelector('.editor');"
            " const ar=a.getBoundingClientRect(), er=e.getBoundingClientRect();"
            " return JSON.stringify({dir:getComputedStyle(s).flexDirection,"
            "  asideW:Math.round(ar.width), editorTop:Math.round(er.top), asideBottom:Math.round(ar.bottom),"
            "  stacked: er.top >= ar.bottom - 2}); })()"
        )
        lay = json.loads(layout or "{}")
        print(f"3) 人设布局: flex-direction={lay.get('dir')} aside宽={lay.get('asideW')} "
              f"上下堆叠={lay.get('stacked')}")
        if lay.get("dir") != "column":
            fails.append(f"人设页没改成单列（flex-direction={lay.get('dir')}）")
        if not lay.get("stacked"):
            fails.append("人设页的列表与编辑器没有上下堆叠")

        # ---- 4) 片段表转卡片（选一个人设让表格出现） ----
        # ⚠️ 人设列表是异步加载的 → **等它出现再点**，否则点了个空气，
        #    然后判据"看起来通过了"（这个坑真的踩过：空片段表的 bug 被漏掉）。
        picked = ""
        bound_pid = ""
        for _ in range(20):
            picked = cdp.eval(
                "(() => { const li=document.querySelector('#persona-list li');"
                " if(!li) return ''; li.click(); return li.textContent.trim(); })()"
            ) or ""
            if picked:
                break
            time.sleep(0.5)
        print(f"   选中人设: {picked or '（没有人设！）'}")
        if not picked:
            fails.append("人设列表是空的 —— 无法验证片段表（先导入一个人设）")
        else:
            # 手机端点开会"路由到详情"→ hash 里有人设 id（`#/persona/<id>`）。
            # ⚠️ 从**真 URL** 取，不去碰模块级 `state`（它不在 window 上）。
            bound_pid = str(
                cdp.eval(
                    "(function(){var m=/^#\\/persona\\/([^\\/]+)/.exec(location.hash);"
                    " return m?decodeURIComponent(m[1]):'';})()"
                )
                or ""
            )
            print(f"   人设 id: {bound_pid or '（拿不到，判据会失败）'}")
            if not bound_pid:
                fails.append("拿不到选中人设的 id（无法验证片段数据，不算通过）")

        time.sleep(1.5)
        tbl = cdp.eval(
            "(() => { const th=document.querySelector('table.sections thead');"
            " const td=document.querySelector('table.sections tbody td');"
            " if(!td) return JSON.stringify({noTable:true});"
            " return JSON.stringify({"
            "  theadHidden: th ? getComputedStyle(th).display === 'none' : null,"
            "  tdDisplay: getComputedStyle(td).display,"
            "  label: td.getAttribute('data-label'),"
            "  beforeContent: getComputedStyle(td,'::before').content }); })()"
        )
        t = json.loads(tbl or "{}")
        print(f"4) 片段表: {t}")
        if t.get("noTable"):
            # ⚠️ 不允许静默跳过：这条判据此前就是"没选到人设 → 跳过 → 报通过"，
            #    结果**真正的空片段表 bug 被漏掉了**。现在必须选到才算过。
            fails.append("没选到人设，片段表判据未验证（不能算通过）")
        else:
            if not t.get("theadHidden"):
                fails.append("片段表表头在手机上没隐藏")
            # 卡片模式：td 变成 flex（标签 + 控件并排）或 block 都算对
            if t.get("tdDisplay") not in ("flex", "block"):
                fails.append(f"片段表格没转成卡片（td display={t.get('tdDisplay')}）")
            # 字段名必须由 CSS ::before 提供（表头已隐藏，否则用户不知道这行是什么）
            if not t.get("label") or '"' not in (t.get("beforeContent") or ""):
                fails.append(
                    f"片段卡片没显示字段名（label={t.get('label')!r} "
                    f"::before={t.get('beforeContent')!r}）"
                )
            # 行数 > 0：光有表结构没内容是**假通过**
            #
            # ⚠️ 2026-09-21 改：人设页的片段表**只显示"额外片段"**了
            #    （情绪要求去掉与上方 4 个字段重复的那几行，见
            #    `app.js` 的 `PERSONA_CORE_TITLES` 注释）—— 而"糯米茨"
            #    这个人设**只有** 4 个核心字段、没有额外片段，
            #    所以表里现在**正常地**是 0 行。
            #
            #    🔴 但这条判据当年抓的是**真 bug**：
            #       "导入的人设没有物化出片段 → 表是空的"。
            #       所以**不能删**，要把它重新指向**同一个被测性质**：
            #       判据从"表里有几行"改成"**这个人设到底有没有片段**" ——
            #       数据里一段都没有才算失败（那才是当年那个 bug）。
            rows = cdp.eval("document.querySelectorAll('#sections-body tr').length") or 0
            has_empty_note = bool(
                cdp.eval(
                    "!!document.querySelector('#sections-body tr.empty-row')"
                )
            )
            real_sections = cdp.eval(
                # 真 HTTP：直接问后端这个人设有几段（0 段 = 当年那个 bug）
                "(async function(){var r=await fetch('/api/v1/personas/'+encodeURIComponent("
                + json.dumps(str(bound_pid or ""))
                + ")); if(!r.ok) return -1; var d=await r.json();"
                " return ((d.prompt_sections)||[]).length;})()",
                await_promise=True,
            )
            print(
                f"   额外片段行数: {rows}（空态提示={has_empty_note}）"
                f" 数据里的片段总数: {real_sections}"
            )
            if rows == 0 and not has_empty_note:
                # 0 行**且没有空态说明** = 表格渲染坏了（两种正常情况都不是）
                fails.append(
                    "片段表 0 行且没有空态说明 —— 表格没渲染出来"
                    "（不是『没有额外片段』那种正常空）"
                )
            if real_sections == 0:
                fails.append(
                    "这个人设在数据里**一段片段都没有**"
                    "（导入的人设没有物化出片段 —— 这正是本判据当年抓到的 bug）"
                )
            elif real_sections < 0:
                fails.append("读人设片段数失败（HTTP 非 2xx）—— 判据未验证，不算通过")

        # ---- 5) 输入框字号（iOS 聚焦不放大） ----
        fs = cdp.eval(
            "(() => { const i=document.querySelector('#p-name');"
            " return i ? parseFloat(getComputedStyle(i).fontSize) : null; })()"
        )
        print(f"5) 输入框字号: {fs}px")
        if fs is not None and fs < 16:
            fails.append(f"输入框字号 {fs}px < 16px（iOS 聚焦会自动放大）")

        # ---- 截图 ----
        shot = cdp.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        if shot.get("data"):
            with open("/tmp/mobile_persona.png", "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            print("   截图 → /tmp/mobile_persona.png")

        # 聊天页也截一张
        cdp.eval("document.querySelector('.tab[data-tab=\"chat\"]').click()")
        time.sleep(1.0)
        shot2 = cdp.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        if shot2.get("data"):
            with open("/tmp/mobile_chat.png", "wb") as f:
                f.write(base64.b64decode(shot2["data"]))
            print("   截图 → /tmp/mobile_chat.png")

        cdp.close()

        if fails:
            print("\n❌ 失败：")
            for f_ in fails:
                print("   -", f_)
            return 1
        print("\n✅ 通过：手机端 5 项判据全部满足")
        return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()


if __name__ == "__main__":
    sys.exit(main())
