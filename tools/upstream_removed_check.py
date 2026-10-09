#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「上游连接」已移除的真浏览器判据 + **删掉之后别处没被带坏**。

## 为什么要专门写这一批
删一块 UI 的风险**不在于那块本身**，而在于：
  ① 它的 JS 处理器还留着 → `$("x").onclick` 在元素不存在时**当场抛 TypeError**
     → 整个脚本后半段（模型供应商页/插件页/日志页的全部接线）**一起死掉**；
  ② 别的页面**依赖它顺带留下的东西**（比如 `loadSettings` 还负责渲染部署配置、
     还负责拿 `active_persona_id`）→ 删过头就静默少一块；
  ③ 「用哪个模型」的引导断了 → 用户不知道该去哪配。

## 判据分组
  A. 上游连接**确实没了**（且没留半截控件）
  B. **后面的页面全都还活着**（B 组是核心：判据要能证明"脚本没在中间断掉"）
  C. 设置页仍拿到「当前人设」（`loadSettings` 删过头就会丢）
  D. 零 JS 异常
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
ROOT = Path(__file__).resolve().parent.parent


def _token() -> str:
    m = re.search(r"^EMOTION_AUTH_ACCESS_TOKEN=(.+)$", (ROOT / ".env").read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("❌ .env 里没有 EMOTION_AUTH_ACCESS_TOKEN")
    return m.group(1).strip().strip("'").strip('"')


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

    def eval(self, expr: str, *, aw: bool = False):
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=aw, returnByValue=True)
        return None if "exceptionDetails" in r else r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


_WAIT = """(async () => {
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 100));
    if (!window.__urBusy) return true;
  }
  return false;
})()"""

_INSTALL = """(() => {
  if (window.__urInstalled) return true;
  window.__urInstalled = true;
  window.__urLive = 0; window.__urBusy = false;
  const of = window.fetch;
  window.fetch = function (...a) {
    window.__urLive++; window.__urBusy = true;
    return of.apply(this, a).finally(() => {
      window.__urLive--;
      if (window.__urLive <= 0) { window.__urLive = 0;
        setTimeout(() => { window.__urBusy = false; }, 250); }
    });
  };
  return true;
})()"""


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = _token()
    port = 9264
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}", "--no-sandbox",
         "--disable-gpu", "--disable-dev-shm-usage", "--remote-allow-origins=*",
         "--window-size=1440,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    fails: list[str] = []
    js_errors: list[str] = []
    try:
        target = None
        for _ in range(40):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as f:
                    target = next((t for t in json.loads(f.read()) if t["type"] == "page"), None)
                if target:
                    break
            except Exception:
                continue
        if not target:
            print("❌ Chrome 未就绪")
            return 1

        cdp = CDP(target["webSocketDebuggerUrl"])
        cdp.send("Page.enable"); cdp.send("Runtime.enable"); cdp.send("Log.enable")

        def drain():
            cdp.ws.settimeout(0.01)
            try:
                while True:
                    msg = json.loads(cdp.ws.recv())
                    if msg.get("method") == "Runtime.exceptionThrown":
                        js_errors.append(str(msg["params"]["exceptionDetails"].get("exception", {}).get("description"))[:220])
                    elif msg.get("method") == "Log.entryAdded":
                        e = msg["params"]["entry"]
                        if e.get("level") == "error":
                            js_errors.append(str(e.get("text"))[:220])
            except Exception:
                pass
            finally:
                cdp.ws.settimeout(120)

        cdp.send("Page.navigate", url=base)
        time.sleep(2.2)
        cdp.eval(_INSTALL)
        cdp.eval("document.getElementById('login-token').value=%s;"
                 "document.getElementById('login-form').requestSubmit();" % json.dumps(token))
        cdp.eval(_WAIT, aw=True); time.sleep(1.0); cdp.eval(_WAIT, aw=True)

        def check(label, expr, *, aw=False):
            raw = cdp.eval(
                ("(async () => { const r = await " + expr + "; return JSON.stringify(r); })()") if aw
                else ("(() => { const r = " + expr + "; return JSON.stringify(r); })()"),
                aw=True,
            )
            if raw is None:
                print(f"  ❌ {label} —— 求值异常")
                fails.append(label); return False
            r = json.loads(raw)
            print(f"  {'✅' if r.get('ok') else '❌'} {label} —— {r.get('detail')}")
            if not r.get("ok"):
                fails.append(label)
            return bool(r.get("ok"))

        print("\n================ A 上游连接确实没了 ================")
        cdp.eval("location.hash='#/settings'")
        cdp.eval(_WAIT, aw=True)

        check("A1 那块表单的元素一个都不剩", """(() => {
              // 🔴 清单要**列全**：我第一版漏了 `ping`（它排在最后），
              //    注入"把测试连接按钮加回 HTML"时判据 **8 条全绿**（MISSED）。
              //    这是"判据只覆盖了它想说的事情的一部分，而它看起来是绿的"。
              const gone = ['s-base-url','s-model','s-api-key','model-picker',
                            'model-picker-wrap','fetch-models','clear-overrides',
                            'settings-form','ping',
                            // 顺带把整块的容器也点上：万一将来有人把 `<form>` 留个空壳
                            // 而只删里面的 input，这条会红。
                            's-api-key'];
              const uniq = [...new Set(gone)];
              const left = gone.filter(id => document.getElementById(id));
              return { ok: left.length === 0,
                       detail: left.length ? ('残留: ' + left.join(',')) : (uniq.length + ' 个元素都已移除') };
            })()""")

        check("A2 页面开头有引导（去哪配模型）", """(() => {
              const panel = document.getElementById('tab-settings');
              const t = (panel.innerText || '');
              const has = t.includes('模型供应商') && t.includes('会话');
              return { ok: has, detail: has ? '引导文案在' : ('没找到引导，前 120 字：' + t.slice(0,120)) };
            })()""")

        print("\n================ B 后面的页面全都还活着（核心） ================")
        # ⚠️ B 组是**这一批的要点**：如果那些 `$("x").onclick` 没删干净，
        #    脚本会在中途抛错 → 从这里往后**所有接线都不会执行**。
        #    所以要真的去点、真的看它有没有反应，而不是"元素存在"。
        # ⚠️ 元素名要照**真 HTML** 取，别照记忆猜（我第一版写了 `ps-source-list`，
        #    真名是 `ps-configured-list` → 判据假红，而产品是好的）。
        #    更稳的判据是断言"**源行真的渲染出来了**"，不只是"容器在" ——
        #    容器是写死在 HTML 里的，脚本中途断掉它照样在。
        check("B1 模型供应商页：源列表真的渲染出来了", """(async () => {
              location.hash = '#/providers';
              for (let i = 0; i < 50; i++) {
                await new Promise(r => setTimeout(r, 100));
                if (!document.getElementById('tab-providers').classList.contains('hidden')) break;
              }
              // ⚠️ 要**等异步渲染落定**再断言：设 hash 只触发路由，取数是异步的。
              //    抢跑会让判据读到尚未渲染的空列表（假红），
              //    或者读到渲染完的旧内容（假绿）—— 两个方向都错。
              let rows = [];
              for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 100));
                rows = [...document.querySelectorAll('#provider-source-list > *')];
                if (rows.length > 0) break;
              }
              // 🔴 判据要断言"**行里真有一份非空内容**"，不只是"有几个元素"。
              //    我第一版只数个数：注入"渲染完再清空"时 **8 条判据全绿**（MISSED）
              //    —— 因为清空是一次 `innerHTML=""`，行数确实变 0，
              //    但我那次注入跑在**渲染之后**、判据读的又是更早的快照。
              //    改成"等到有行 **且** 行文本非空"才把这件事守住。
              const text = rows.map(r => (r.innerText || '').trim()).join('');
              return {
                ok: rows.length > 0 && text.length > 0,
                detail: rows.length + ' 个源行，文本 ' + text.length + ' 字符'
                        + (rows.length && !text.length ? '（行在但内容是空的）' : '')
              };
            })()""", aw=True)

        check("B2 日志页：刷新按钮点得动且真发请求", """(async () => {
              location.hash = '#/logs';
              for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 100));
                if (!document.getElementById('tab-logs').classList.contains('hidden')) break;
              }
              const btn = document.getElementById('logs-refresh');
              if (!btn) return { ok: false, detail: '没有 #logs-refresh' };
              // 🔴 判据不是"按钮在"，而是"**点了之后真发了请求**"——
              //    `$()` 返回 null 时 `onclick=` 那行会抛错，按钮看着在、点了没反应。
              const before = window.__urLive;
              let fired = false;
              const of = window.fetch;
              window.fetch = function (...a) { fired = true; return of.apply(this, a); };
              btn.click();
              await new Promise(r => setTimeout(r, 800));
              window.fetch = of;
              return { ok: fired, detail: fired ? '点「立即刷新」真的发出了请求' : '点了没反应（onclick 没接上）' };
            })()""", aw=True)

        check("B3 插件页：插件列表渲染出来了", """(async () => {
              location.hash = '#/plugins';
              for (let i = 0; i < 50; i++) {
                await new Promise(r => setTimeout(r, 100));
                if (!document.getElementById('tab-plugins').classList.contains('hidden')) break;
              }
              await new Promise(r => setTimeout(r, 900));
              const rows = document.querySelectorAll('#plugin-switch-list .switch-row').length;
              return { ok: rows > 0, detail: rows + ' 个插件行（0 = 脚本断在中间了）' };
            })()""", aw=True)

        print("\n================ C 别处依赖的东西还在 ================")
        check("C1 部署配置（/health 渲染）仍在", """(() => {
              const el = document.getElementById('deploy-config');
              const t = el ? (el.textContent || '').trim() : '';
              return { ok: t.length > 10 && t.includes('{'),
                       detail: '部署配置 ' + t.length + ' 字符' };
            })()""")

        check("C2 设置页的会话模型下拉仍有真实选项", """(() => {
              const host = document.getElementById('cfg-session-model-fields');
              const sel = host && host.querySelector('select');
              if (!sel) return { ok: false, detail: '模型下拉不在了' };
              const real = [...sel.options].filter(o => o.value !== '');
              return { ok: real.length > 0, detail: '可选 ' + real.length + ' 个' };
            })()""")

        drain()
        print("\n================ D 零 JS 异常 ================")
        real = [e for e in js_errors if e and e != "None"]
        if real:
            print(f"  ❌ 捕获 {len(real)} 条：")
            for e in real[:6]:
                print(f"     - {e[:180]}")
            fails.append("D JS 异常")
        else:
            print("  ✅ 全程零 console 错误")
        cdp.close()
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()

    if fails:
        print("\n❌ 失败项：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
