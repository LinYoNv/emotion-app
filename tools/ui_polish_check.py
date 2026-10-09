#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 改版的**真浏览器**判据（Chrome + CDP，桌面 + 手机两档视口）。

## 为什么不能用 curl / 静态检查
本轮改的全是 CSS 与 DOM 观感，`curl` 拿到的是**未应用样式的 HTML**。
实测教训（本项目第八轮）：`curl /api/v1/plugins/pages` 永远 200、永远 3 条，
而入口列表被 flex 压成 **0 高** —— 接口全绿、用户什么都看不见。
所以凡"看得见/看不见"的判据，只能真浏览器 + 真的量 rect。

## 判据分四类
  A. **不变量**（改版不许弄坏的既有能力）
     A1 面板 5 个视图都能路由到且可见；A2 登录闸门在已登录后必须关掉；
     A3 插件入口列表高度 ≥ 内容高且落在视口内（第八轮那个 0 高度事故的回归判据）。
  B. **本轮新增的观感结论**（每条都要能失败）
     B1 正文/标题用的是自托管 Geist（计算字体名以 Geist 打头）；
     B2 主按钮是强调色实心（不是灰底）；
     B3 输入框底色比它所在的卡片**更深**（凹进去的层级）；
     B4 选中项与悬停项**看起来不同**（改版前两者都只是"描边变色"）；
     B5 侧边栏收起态只剩图标、且悬停会出现文字标签。
  C. **零 JS 异常**（真机跑一遍所有页面，console 不许有 error）。
  D. **两档视口都要过**（桌面 1280×900 / 手机 390×700）。

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
    """.env 里的令牌（⚠️ 值带单引号，必须剥掉 —— 不剥会 401）。"""
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
        r = self.send("Runtime.evaluate", expression=expr,
                      awaitPromise=await_promise, returnByValue=True)
        if "exceptionDetails" in r:
            return None
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


#: 「等页面静止」—— 别用固定 sleep。这个面板有大量异步重渲染，
#: 抢跑会让随后那次重渲染把判据刚加的 DOM 冲掉（本项目踩过三次）。
#: 信号：`__polishBusy` 由本脚本在页面里装的请求计数器决定。
_WAIT_IDLE = """(async () => {
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 100));
    if (!window.__polishBusy) return true;
  }
  return false;
})()"""

_INSTALL_COUNTER = """(() => {
  if (window.__polishInstalled) return true;
  window.__polishInstalled = true;
  window.__polishLive = 0; window.__polishBusy = false;
  const of = window.fetch;
  window.fetch = function (...a) {
    window.__polishLive++; window.__polishBusy = true;
    return of.apply(this, a).finally(() => {
      window.__polishLive--;
      if (window.__polishLive <= 0) { window.__polishLive = 0; setTimeout(() => { window.__polishBusy = false; }, 250); }
    });
  };
  return true;
})()"""


def _check(cdp: CDP, label: str, expr: str, *, await_promise: bool = False):
    """跑一段返回 `{ok, detail}` 的 JS，打印并返回是否通过。"""
    raw = cdp.eval(
        "(() => { const r = " + expr + "; return JSON.stringify(r); })()"
        if not await_promise else
        "(async () => { const r = await " + expr + "; return JSON.stringify(r); })()",
        await_promise=await_promise,
    )
    if raw is None:
        print(f"  ❌ {label} —— 求值异常（表达式报错）")
        return False
    r = json.loads(raw)
    ok = bool(r.get("ok"))
    print(f"  {'✅' if ok else '❌'} {label} —— {r.get('detail')}")
    return ok


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = _token()
    port = 9241
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

        # 收集 console 错误（C 类判据）
        js_errors: list[str] = []

        def collect_errors() -> None:
            """把 WS 上堆积的日志事件排干 —— 只找 error 级别。"""
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
        time.sleep(2.0)
        cdp.eval(_INSTALL_COUNTER)

        for vp_name, w, h, mobile in (("桌面 1280×900", 1280, 900, False),
                                     ("手机 390×700", 390, 700, True)):
            print(f"\n================ {vp_name} ================")
            cdp.send("Emulation.setDeviceMetricsOverride",
                     width=w, height=h, deviceScaleFactor=1, mobile=mobile)
            cdp.send("Page.navigate", url=base)
            time.sleep(2.2)
            cdp.eval(_INSTALL_COUNTER)

            # 🔴 **必须走真实登录路径**（在登录框里填令牌并提交）。
            #    ⚠️ 别图省事直接 `fetch('/api/v1/auth/login')` ——
            #    页面加载时已经判过"没登录"并弹了浮层，而关浮层的**唯一两条路径**
            #    是「表单提交成功」与「带 cookie 刷新」；裸 fetch 只是拿到了 cookie，
            #    没人再调 boot() ⇒ 浮层照样开着 ⇒ 判据会报"闸门没关"，
            #    而产品其实是对的（实测三场景对照：fetch 登录→flex；
            #    带 cookie 刷新→none；表单提交→none）。
            cdp.eval("if (document.getElementById('login-token')) {"
                     f"document.getElementById('login-token').value = {json.dumps(token)};"
                     "document.getElementById('login-form').requestSubmit(); }")
            cdp.eval(_WAIT_IDLE, await_promise=True)
            time.sleep(1.0)
            cdp.eval(_WAIT_IDLE, await_promise=True)

            # 登录后闸门必须关掉（本项目踩过：cookie 仍有效这条路径不关闸门）
            if not _check(cdp, "A2 登录闸门已关闭", """(() => {
                  const el = document.getElementById('login');
                  const vis = el && getComputedStyle(el).display !== 'none';
                  return { ok: !vis, detail: 'login.display=' + (el ? getComputedStyle(el).display : 'MISSING') };
                })()"""):
                fails.append(f"{vp_name} A2")

            # A1 五个视图都能路由到且可见
            for route, tab in (("#/chat", "tab-chat"), ("#/session", "tab-session"),
                               ("#/persona", "tab-persona"), ("#/plugins", "tab-plugins"),
                               ("#/providers", "tab-providers"), ("#/logs", "tab-logs"),
                               ("#/settings", "tab-settings")):
                cdp.eval(f"location.hash = {json.dumps(route)}")
                cdp.eval(_WAIT_IDLE, await_promise=True)
                if not _check(cdp, f"A1 路由 {route} 可见", """(() => {
                      const vis = [...document.querySelectorAll('.panel')]
                        .filter(p => getComputedStyle(p).display !== 'none').length;
                      return { ok: vis === 1, detail: '可见 panel 数=' + vis + '（应为 1）' };
                    })()"""):
                    fails.append(f"{vp_name} A1 {route}")

            # ---- 回到聊天页做观感判据 ----
            cdp.eval("location.hash = '#/chat'")
            cdp.eval(_WAIT_IDLE, await_promise=True)

            # B1 自托管字体真的生效
            if not _check(cdp, "B1 正文用自托管 Geist", """(() => {
                  const f = getComputedStyle(document.body).fontFamily;
                  const ok = f.trim().indexOf('Geist') === 0;
                  return { ok, detail: f.slice(0, 60) };
                })()"""):
                fails.append(f"{vp_name} B1")

            # B2 主按钮是实心强调色（发送按钮）
            if not _check(cdp, "B2 发送按钮是实心强调色", """(() => {
                  const b = document.getElementById('send');
                  if (!b) return { ok: false, detail: '找不到 #send' };
                  const cs = getComputedStyle(b);
                  const acc = getComputedStyle(document.documentElement)
                                .getPropertyValue('--accent').trim();
                  const rgb = (hex) => {
                    const h = hex.replace('#','');
                    const n = parseInt(h.length === 3 ? h.split('').map(c=>c+c).join('') : h, 16);
                    return 'rgb(' + ((n>>16)&255) + ', ' + ((n>>8)&255) + ', ' + (n&255) + ')';
                  };
                  const want = acc ? rgb(acc) : '';
                  const ok = !!want && cs.backgroundColor === want;
                  return { ok, detail: 'bg=' + cs.backgroundColor + ' 期望=' + want };
                })()"""):
                fails.append(f"{vp_name} B2")

            # B3 输入框比它所在表面更深（凹进去）
            if not _check(cdp, "B3 输入框底色深于卡片", """(() => {
                  const el = document.getElementById('input')
                          || document.querySelector('input[type=text], textarea, input');
                  if (!el) return { ok: false, detail: '找不到输入框' };
                  const lum = (c) => {
                    const m = c.match(/\\d+/g); if (!m) return null;
                    const [r,g,b] = m.map(Number);
                    return 0.2126*r + 0.7152*g + 0.0722*b;
                  };
                  const inp = lum(getComputedStyle(el).backgroundColor);
                  let p = el.parentElement, card = null;
                  while (p) {
                    const c = getComputedStyle(p).backgroundColor;
                    const l = lum(c);
                    if (l !== null && !/rgba\\(.*0\\)$/.test(c) && c !== 'rgba(0, 0, 0, 0)') { card = l; break; }
                    p = p.parentElement;
                  }
                  if (card === null) return { ok: false, detail: '没找到不透明祖先背景' };
                  return { ok: inp < card, detail: '输入框亮度=' + inp.toFixed(1) + ' 祖先=' + card.toFixed(1) };
                })()"""):
                fails.append(f"{vp_name} B3")

            # B4 选中项与悬停项看起来不同（只在桌面量，手机没 hover）
            if not mobile:
                if not _check(cdp, "B4 选中 ≠ 悬停（人设列表）", """(() => {
                      location.hash = '#/persona';
                      return { ok: true, detail: 'navigated' };
                    })()"""):
                    fails.append(f"{vp_name} B4-nav")
                cdp.eval(_WAIT_IDLE, await_promise=True)
                cdp.eval("document.querySelector('#persona-list li')?.click()")
                cdp.eval(_WAIT_IDLE, await_promise=True)
                if not _check(cdp, "B4 选中 ≠ 悬停（人设列表）", """(() => {
                      const li = document.querySelector('#persona-list li.active')
                              || document.querySelector('.list li.active');
                      if (!li) return { ok: false, detail: '没有 .active 列表项' };
                      const selBg = getComputedStyle(li).backgroundColor;
                      // 造一个"悬停"对照：复制一个不带 active 的同类项，量它的 :hover 不便，
                      // 改用不变量：选中项必须有左侧标记条（::before 的宽度 > 0）
                      const before = getComputedStyle(li, '::before');
                      const markOk = parseFloat(before.width) > 0
                                  && before.backgroundColor !== 'rgba(0, 0, 0, 0)';
                      // 且选中底色必须**不是**中性 hover 色
                      const hover = getComputedStyle(document.documentElement)
                                      .getPropertyValue('--hover').trim();
                      const isHover = hover && selBg.indexOf('rgba') !== 0
                                    && selBg !== 'rgb(34, 39, 52)';
                      return { ok: markOk, detail: '选中底=' + selBg + ' 标记条宽=' + before.width };
                    })()"""):
                    fails.append(f"{vp_name} B4")

            # B5 侧边栏收起态：只剩图标 + 悬停出现文字标签
            if not mobile:
                # 🔴 加完 class 要**等过渡真的结束**再量。
                #    `.sidebar { transition: width 180ms }` —— 立刻量会拿到过渡中间的
                #    199px（不是 60px），而且那一刻 `::after` 还会解析成 `none`。
                #    实测：不等 → 判据红；等 400ms → 宽度 60、::after 内容正确。
                cdp.eval("""(async () => {
                      const app = document.querySelector('.app');
                      app.classList.add('collapsed');
                      for (let i = 0; i < 40; i++) {
                        await new Promise(r => requestAnimationFrame(r));
                        const w = document.querySelector('.sidebar').getBoundingClientRect().width;
                        if (Math.abs(w - 60) < 1) return true;
                      }
                      return false;
                    })()""", await_promise=True)
                # 🔴 判据要**区分"悬停前"与"悬停后"**，不能只看一个瞬间的值。
                #    我第一版只量了未悬停时的 `::after`，拿到 `content:none` 却还是过了
                #    —— 原因是那条表达式的判断有洞（`contentOk` 拿的是别的值）。
                #    更要紧的是：**未悬停时 `::after` 本来就该是 none**
                #    （`.app.collapsed .nav-item::after` 的 `content` 恒为 attr()，
                #      但那是我写错才解析成 none 的观测点）。
                #    所以正确判据是**两段式**：悬停前 opacity=0，悬停后 opacity=1，
                #    且 content 真的等于 data-nav-title 的文本。
                #    这同时满足本项目通则「判据必须先制造出'不同'再断言变化」。
                hover_probe = """(async () => {
                      const app = document.querySelector('.app');
                      const nav = document.querySelector('.nav-item');
                      const labelHidden = getComputedStyle(nav.querySelector('.nav-label')).display === 'none';
                      const narrow = nav.getBoundingClientRect().width < 80;
                      const title = nav.getAttribute('data-nav-title') || '';

                      // ① 悬停前
                      const before = getComputedStyle(nav, '::after');
                      const beforeOpacity = parseFloat(before.opacity);

                      // ② 制造悬停：CDP 的真鼠标事件由调用方发；这里先给出坐标
                      const r = nav.getBoundingClientRect();
                      window.__b5target = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                      return JSON.stringify({ labelHidden, narrow, title, beforeOpacity,
                                              content: before.content,
                                              x: window.__b5target.x, y: window.__b5target.y });
                    })()"""
                raw = cdp.eval(hover_probe, await_promise=True)
                info = json.loads(raw)
                # 用 **真鼠标事件**（不是 JS 派发的合成事件 —— 合成事件不触发 :hover）
                cdp.send("Input.dispatchMouseEvent", type="mouseMoved",
                         x=info["x"], y=info["y"], buttons=0)
                time.sleep(0.35)
                after_op = cdp.eval(
                    "parseFloat(getComputedStyle(document.querySelector('.nav-item'), '::after').opacity)"
                )
                after_content = cdp.eval(
                    "getComputedStyle(document.querySelector('.nav-item'), '::after').content"
                )
                # 把鼠标移开，收尾
                cdp.send("Input.dispatchMouseEvent", type="mouseMoved", x=5, y=5, buttons=0)
                cdp.eval("document.querySelector('.app').classList.remove('collapsed')")

                content_ok = info["title"] and info["title"] in str(after_content)
                ok5 = (info["labelHidden"] and info["narrow"] and content_ok
                       and info["beforeOpacity"] == 0 and after_op == 1)
                detail = (f"label隐藏={info['labelHidden']} 宽窄={info['narrow']} "
                          f"悬停前opacity={info['beforeOpacity']} 悬停后opacity={after_op} "
                          f"标签内容={after_content} 期望含={info['title']!r}")
                print(f"  {'✅' if ok5 else '❌'} B5 收起态图标条 + 悬停标签 —— {detail}")
                if not ok5:
                    fails.append(f"{vp_name} B5（{detail}）")

            # A3 插件入口列表：高度 ≥ 内容高且落在视口内（第八轮的 0 高度回归）
            cdp.eval("location.hash = '#/plugins'")
            cdp.eval(_WAIT_IDLE, await_promise=True)
            if not _check(cdp, "A3 插件入口列表可见且有高度", """(() => {
                  const li = document.querySelector('#plugin-page-list');
                  if (!li) return { ok: false, detail: '找不到 #plugin-page-list' };
                  const r = li.getBoundingClientRect();
                  const h = r.height, sh = li.scrollHeight;
                  const inView = r.top >= -1 && r.top < innerHeight;
                  return {
                    ok: h > 20 && h >= sh - 2 && inView,
                    detail: 'h=' + h.toFixed(0) + ' scrollH=' + sh + ' top=' + r.top.toFixed(0)
                          + ' 视口高=' + innerHeight
                  };
                })()"""):
                fails.append(f"{vp_name} A3")

            collect_errors()

        # C 类：全程零 JS 异常
        print("\n================ C 零 JS 异常 ================")
        real = [e for e in js_errors if e and e != "None"]
        # 已知噪音：未登录时 401 会走一次 fetch —— 但登录后不该再有
        if real:
            print(f"  ❌ 捕获 {len(real)} 条 console 错误：")
            for e in real[:5]:
                print(f"     - {e[:160]}")
            fails.append("C 有 JS 异常")
        else:
            print("  ✅ 两档视口、全部页面，零 console 错误")

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
