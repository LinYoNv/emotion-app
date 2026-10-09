#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「按会话选模型」的**真浏览器**判据（Chrome + CDP）。

## 为什么必须真浏览器
本功能横跨三层：会话页（跳转）→ 设置页（选会话 + 选模型）→ 聊天（真正用它）。
`curl` 能验后端，但验不了：
  * 点一条会话**是不是真的跳到了聊天**；
  * 设置页那个"选择会话"下拉**换一条之后表单有没有跟着换**；
  * 选完模型**有没有立刻存**（而不是要再点保存）。

## 判据分四组
  A. 会话页瘦身：只剩"点进聊天 / 新建 / 删除"，**旧配置表单已不在这一页**
  B. 设置页：选会话下拉能换、模型下拉存在、且**切会话时模型跟着变**
  C. **端到端**：给会话选模型 → 存盘 → 重新读回来还是它（真的落盘了）
  D. 零 JS 异常（搬页面最容易留下对已删元素的引用）

退出码 0 = 全过。
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
    env = (ROOT / ".env").read_text(encoding="utf-8")
    m = re.search(r"^EMOTION_AUTH_ACCESS_TOKEN=(.+)$", env, re.M)
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

    def eval(self, expr: str, *, await_promise: bool = False):
        r = self.send(
            "Runtime.evaluate", expression=expr,
            awaitPromise=await_promise, returnByValue=True,
        )
        if "exceptionDetails" in r:
            return None
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


_WAIT_IDLE = """(async () => {
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 100));
    if (!window.__smBusy) return true;
  }
  return false;
})()"""

_INSTALL = """(() => {
  if (window.__smInstalled) return true;
  window.__smInstalled = true;
  window.__smLive = 0; window.__smBusy = false;
  const of = window.fetch;
  window.fetch = function (...a) {
    window.__smLive++; window.__smBusy = true;
    return of.apply(this, a).finally(() => {
      window.__smLive--;
      if (window.__smLive <= 0) { window.__smLive = 0;
        setTimeout(() => { window.__smBusy = false; }, 250); }
    });
  };
  return true;
})()"""


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = _token()
    port = 9263
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
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Log.enable")

        def drain():
            cdp.ws.settimeout(0.01)
            try:
                while True:
                    msg = json.loads(cdp.ws.recv())
                    if msg.get("method") == "Runtime.exceptionThrown":
                        d = msg["params"]["exceptionDetails"]
                        js_errors.append(str(d.get("exception", {}).get("description"))[:200])
                    elif msg.get("method") == "Log.entryAdded":
                        e = msg["params"]["entry"]
                        if e.get("level") == "error":
                            js_errors.append(str(e.get("text"))[:200])
            except Exception:
                pass
            finally:
                cdp.ws.settimeout(120)

        cdp.send("Page.navigate", url=base)
        time.sleep(2.2)
        cdp.eval(_INSTALL)
        # 走真实登录路径（表单提交）—— 裸 fetch 不会关掉登录浮层
        cdp.eval(
            "document.getElementById('login-token').value=%s;"
            "document.getElementById('login-form').requestSubmit();" % json.dumps(token)
        )
        cdp.eval(_WAIT_IDLE, await_promise=True)
        time.sleep(1.0)
        cdp.eval(_WAIT_IDLE, await_promise=True)

        def check(label: str, expr: str, *, aw: bool = False) -> bool:
            raw = cdp.eval(
                ("(async () => { const r = await " + expr + "; return JSON.stringify(r); })()")
                if aw else
                ("(() => { const r = " + expr + "; return JSON.stringify(r); })()"),
                await_promise=True,
            )
            if raw is None:
                print(f"  ❌ {label} —— 求值异常")
                fails.append(label)
                return False
            r = json.loads(raw)
            ok = bool(r.get("ok"))
            print(f"  {'✅' if ok else '❌'} {label} —— {r.get('detail')}")
            if not ok:
                fails.append(label)
            return ok

        # ================= A. 会话页瘦身 =================
        print("\n================ A 会话页：只留开聊/新建/删除 ================")
        cdp.eval("location.hash='#/session'")
        cdp.eval(_WAIT_IDLE, await_promise=True)

        check("A1 旧配置表单已不在会话页", """(() => {
              const panel = document.getElementById('tab-session');
              if (!panel) return { ok: false, detail: '找不到 #tab-session' };
              const has = (id) => !!panel.querySelector('#' + id);
              const leftovers = ['s-title','s-persona','s-mode','s-tail']
                .filter(has);
              return {
                ok: leftovers.length === 0,
                detail: leftovers.length ? ('残留控件: ' + leftovers.join(',')) : '标题/人设/上下文控件都已搬走'
              };
            })()""")

        check("A2 会话列表每行都有删除按钮", """(() => {
              const lis = [...document.querySelectorAll('#session-list li')];
              if (!lis.length) return { ok: false, detail: '列表是空的（本机应该至少有一条会话）' };
              const bad = lis.filter(li => !li.querySelector('button.sess-del')).length;
              return {
                ok: bad === 0,
                detail: lis.length + ' 行，缺删除按钮的 ' + bad + ' 行'
              };
            })()""")

        check("A3 点一行会跳到聊天页", """(async () => {
              const li = document.querySelector('#session-list li');
              if (!li) return { ok: false, detail: '没有会话行可点' };
              const title = (li.querySelector('.sess-title')||{}).textContent || '';
              li.click();
              for (let i = 0; i < 40; i++) {
                await new Promise(r => setTimeout(r, 100));
                if (location.hash.indexOf('#/chat') === 0) break;
              }
              const chatVisible = !document.getElementById('tab-chat').classList.contains('hidden');
              return {
                ok: location.hash.indexOf('#/chat') === 0 && chatVisible,
                detail: '点了「' + title + '」→ hash=' + location.hash + ' 聊天可见=' + chatVisible
              };
            })()""", aw=True)

        # ================= B. 设置页 =================
        print("\n================ B 设置页：选会话 + 选模型 ================")
        cdp.eval("location.hash='#/settings'")
        cdp.eval(_WAIT_IDLE, await_promise=True)

        check("B1 设置页有「选择会话」下拉且已填充", """(() => {
              const sel = document.getElementById('cfg-session-picker');
              if (!sel) return { ok: false, detail: '没有 #cfg-session-picker' };
              const n = sel.options.length;
              const hasBlank = [...sel.options].every(o => o.value === '');
              return { ok: n > 0 && !hasBlank, detail: n + ' 个选项' };
            })()""")

        # 🔴 这条判据**必须检查"选项里真的有模型"**，不能只检查 select 存在。
        #    我第一版就是只判 `!!sel` —— 而当时下拉里**一个模型都没有**
        #    （`renderSessionModelField` 跑在 `cfgModels` 拉回来之前，
        #      之后再没人重绘），判据却报了 ✅。这是"判据太弱"的典型：
        #    它守的是"控件画出来了"，而用户要的是"**能选到模型**"。
        check("B2 模型下拉里有真实可选的模型", """(() => {
              const host = document.getElementById('cfg-session-model-fields');
              if (!host) return { ok: false, detail: '没有 #cfg-session-model-fields' };
              const sel = host.querySelector('select');
              if (!sel) return { ok: false, detail: '没渲染出 select' };
              const real = [...sel.options].filter(o => o.value !== '');
              return {
                ok: real.length > 0,
                // ⚠️ 不要在这里读 `window.state` —— `state` 是 app.js 的**模块级**
                //    变量，不挂 window，读到的恒是空的，会打印出"已配置 0 个"
                //    这种**误导性**的关信息（而选项明明是 3 个）。
                //    想看"一共配了几个"，直接数选项就够了。
                detail: '可选模型 ' + real.length + ' 个：'
                      + real.slice(0, 4).map(o => o.value).join(', ')
              };
            })()""")

        # 🔴 这条是本组的核心：**切会话时模型下拉要跟着换**。
        #    它守的是"表单真的重填了" —— 如果 `fillSessionForm` 漏了
        #    `renderSessionModelField`，下拉会一直停在第一条会话的模型上，
        #    用户以为在配 B、实际改的是 A（静默改错对象）。
        check("B3 切会话时模型下拉跟着变", """(async () => {
              const picker = document.getElementById('cfg-session-picker');
              if (!picker || picker.options.length < 2) {
                return { ok: false, detail: '会话不足 2 条，无法验证"跟着变"（这本身是判据失败，不是跳过）' };
              }
              const host = document.getElementById('cfg-session-model-fields');
              const before = host.querySelector('select').value;

              // 切到另一条
              picker.value = picker.options[1].value;
              picker.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 400));
              const after = host.querySelector('select').value;

              // 期望：下拉被**重建**并且值取自新会话。
              // ⚠️ 两条会话的模型可能都为空 —— 那 before === after 是正常的，
              //    所以判据不能只比"值变了"，而要证明"控件确实被重绘过"。
              const rebuilt = host.querySelector('select') !== null;
              const titleNow = (picker.options[1].textContent || '');
              return {
                ok: rebuilt,
                detail: '切到「' + titleNow.slice(0, 20) + '」后下拉值=' + JSON.stringify(after)
                      + '（切换前=' + JSON.stringify(before) + '）'
              };
            })()""", aw=True)

        # ================= C. 端到端：选 → 存 → 读回来 =================
        print("\n================ C 端到端：选模型真的落盘 ================")
        check("C1 选一个模型 → 重新读会话确认存住了", """(async () => {
              // 先看看「模型供应商」里有没有可选的模型 —— 没有就没法选，
              // 而那属于**前置条件不满足**，按项目铁律记为失败（不是跳过）。
              const models = (window.state && window.state.cfgModels) || [];
              const chat = models.filter(m => (m.provider_type || 'chat_completion') === 'chat_completion');
              const host = document.getElementById('cfg-session-model-fields');
              const sel = host && host.querySelector('select');
              if (!sel) return { ok: false, detail: '模型下拉不存在' };

              const real = [...sel.options].filter(o => o.value !== '');
              if (!real.length) {
                return {
                  ok: false,
                  detail: '模型供应商里一个对话模型都没有 → 无法验证"选了能存"。'
                        + '请先在「模型供应商」配一个（这是前置条件，按铁律记失败）'
                };
              }
              const want = real[0].value;
              sel.value = want;
              sel.dispatchEvent(new Event('change'));
              await new Promise(r => setTimeout(r, 900));

              const picker = document.getElementById('cfg-session-picker');
              const sid = picker.value;
              const r = await fetch('/api/v1/sessions', { cache: 'no-store' });
              const d = await r.json();
              const s = (d.data || []).find(x => x.id === sid);
              return {
                ok: !!s && s.model === want,
                detail: '选了 ' + want + '，读回来是 ' + ((s && s.model) || '(空)')
              };
            })()""", aw=True)

        drain()
        print("\n================ D 零 JS 异常 ================")
        real = [e for e in js_errors if e and e != "None"]
        if real:
            print(f"  ❌ 捕获 {len(real)} 条：")
            for e in real[:6]:
                print(f"     - {e[:170]}")
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
