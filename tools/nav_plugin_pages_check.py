#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侧边栏「插件页面」分组的**真浏览器**判据（情绪 2026-10-09）。

> 情绪原话：「「独立 WebUI」的入口有了啊，在插件页点击就有了，不过做成 astrbot 那样也行，
> 就是在侧边栏添加这个。」「要按我们平台实际有的插件添加，就算以后我自己添加了插件也能自己显示」

## 判据要点（都冲着"静默"那一类去）
  A. **动态生成**：子项来自接口的 `plugins[].pages[]`，**逐条与接口核对**
     —— 守"写死插件名"（情绪明确禁止：以后自己加插件也要能显示）
  B. **显示名**：用 `display_name`（中文名）而不是目录名
  C. **点击能进**：点子项真的打开那个插件页面（且选中态跟上）
  D. **收起态**：不能变成一列没有辨识度的图标
  E. 零 JS 异常
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
  for (let i = 0; i < 80; i++) {
    await new Promise(r => setTimeout(r, 100));
    if (!window.__npBusy) return true;
  }
  return false;
})()"""

_INSTALL = """(() => {
  if (window.__npInstalled) return true;
  window.__npInstalled = true;
  window.__npLive = 0; window.__npBusy = false;
  // 🔴 合成反例开关（见 A5）。默认关闭 ⇒ 判据 A1~A4 跑的是**真实数据**。
  window.__npFake = null;
  const of = window.fetch;
  window.fetch = function (...a) {
    const url = String((a[0] && a[0].url) || a[0] || '');
    // 🔴 只在"合成反例"模式下改写 /api/v1/plugins 的响应，别的一律放行。
    if (window.__npFake && url.indexOf('/api/v1/plugins') === 0 && !url.includes('/pages/')) {
      window.__npLive++; window.__npBusy = true;
      return of.apply(this, a).then(async (r) => {
        const d = await r.clone().json();
        d.plugins = (d.plugins || []).concat(window.__npFake);
        return new Response(JSON.stringify(d), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }).finally(() => {
        window.__npLive--; window.__npBusy = false;
      });
    }
    window.__npLive++; window.__npBusy = true;
    return of.apply(this, a).finally(() => {
      window.__npLive--;
      if (window.__npLive <= 0) { window.__npLive = 0;
        setTimeout(() => { window.__npBusy = false; }, 300); }
    });
  };
  return true;
})()"""


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = _token()
    port = 9266
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

        # 进插件页会顺带加载清单（侧边栏分组与它同源）
        cdp.eval("location.hash='#/plugins'")
        cdp.eval(_WAIT, aw=True); time.sleep(1.2); cdp.eval(_WAIT, aw=True)

        print("\n================ A 动态生成：逐条与接口核对 ================")

        check("A1 侧边栏子项 == 接口里有拓展页的插件（逐条集合比较）", """(async () => {
              const r = await fetch('/api/v1/plugins', { cache: 'no-store' });
              const d = await r.json();
              // 期望：每个「有 pages 的插件 × 它的每个 page」一条
              const want = [];
              for (const p of (d.plugins || [])) {
                for (const pg of (p.pages || [])) want.push(p.id + '/' + pg.name);
              }
              want.sort();
              const drawn = [...document.querySelectorAll('#nav-plugin-items .nav-subitem')]
                              .map(a => a.dataset.navPage).sort();
              const missing = want.filter(x => !drawn.includes(x));
              const extra = drawn.filter(x => !want.includes(x));
              return {
                ok: want.length > 0 && missing.length === 0 && extra.length === 0,
                detail: '接口 ' + want.length + ' 条 / 画出 ' + drawn.length + ' 条'
                        + (missing.length ? ' 少:' + missing.join(',') : '')
                        + (extra.length ? ' 多:' + extra.join(',') : '')
              };
            })()""", aw=True)

        check("A2 用的是中文显示名（不是目录名）", """(() => {
              const items = [...document.querySelectorAll('#nav-plugin-items .nav-subitem')];
              if (!items.length) return { ok: false, detail: '一条子项都没有' };
              const texts = items.map(a => (a.querySelector('.nav-sublabel')||{}).textContent || '');
              // 🔴 必须出现**至少一个** `metadata.yaml` 里的中文显示名 ——
              //    这守的是"读了 display_name 而不是拿目录名当名字"。
              //    ⚠️ 不写死具体是哪个插件（情绪要求"以后自己加插件也能显示"），
              //       所以判据只要求"有中文"且"没有裸露的 astrbot_plugin_ 前缀"。
              const hasCJK = texts.some(t => /[\\u4e00-\\u9fa5]/.test(t));
              const leaked = texts.filter(t => /^astrbot_plugin_/.test(t));
              return {
                ok: hasCJK && leaked.length === 0,
                detail: '子项：' + texts.slice(0, 6).join(' | ')
                        + (leaked.length ? ('  ⚠️ 裸露目录名：' + leaked.join(',')) : '')
              };
            })()""")

        # 🔴🔴 **A4 是这一轮的核心判据**（2026-10-10）。
        #
        # 为什么不能只靠 A1：A1 是"子项 == 接口里有 pages 的插件" ——
        # **两边同源 ⇒ 必然一致**，于是它**永远绿**，证明不了"这个插件该不该被列出来"。
        # 真实事故就在眼前：`meme_manager` 根本没加载成功（`state=failed`，
        # 缺 `requests`），却也出现在导航里、点进去一片空白 —— A1 照样全绿。
        #
        # 所以 A4 引入**外部依据**（两个，都不来自"接口自己说了什么"）：
        #   ① `state` 必须是 `loaded`（跑不起来的插件不该有可点的入口）；
        #   ② 分组归属必须与**模块路径**一致 —— 我们自带的插件是**仓库内顶层包**
        #      （`astrbot_plugin_xxx.main`，`plugins/` 就在仓库根），
        #      外部装进来的必然带命名空间前缀。这条直接守"别把别人的插件
        #      混进「本平台自带」那一栏"。
        check("A4 只列【跑得起来】的插件，且分组归属与模块路径一致", """(async () => {
              const r = await fetch('/api/v1/plugins', { cache: 'no-store' });
              const d = await r.json();
              const byId = {};
              for (const p of (d.plugins || [])) byId[p.id] = p;

              // ① 列出来的必须都是 loaded
              const drawn = [...document.querySelectorAll('#nav-plugin-items .nav-subitem')]
                              .map(a => (a.dataset.navPage || '').split('/')[0]);
              const uniq = [...new Set(drawn)];
              const bad = uniq.filter(id => (byId[id] || {}).state !== 'loaded');

              // ② 分组归属必须与模块路径一致
              const heads = [...document.querySelectorAll('#nav-plugin-items .nav-subhead')]
                              .map(h => (h.textContent || '').trim());
              const boxes = {};
              let cur = null;
              for (const el of document.querySelectorAll('#nav-plugin-items > *')) {
                if (el.classList.contains('nav-subhead')) { cur = (el.textContent||'').trim(); }
                else if (el.dataset && el.dataset.navPage) {
                  (boxes[cur] = boxes[cur] || []).push(el.dataset.navPage.split('/')[0]);
                }
              }
              const wrongGroup = [];
              const isBundled = (id) => /^astrbot_plugin_[A-Za-z0-9_]+\\.main$/.test(
                String((byId[id] || {}).module_path || ''));
              for (const [head, ids] of Object.entries(boxes)) {
                const wantBundled = head.includes('本平台自带');
                const wantExternal = head.includes('外部插件');
                if (!wantBundled && !wantExternal) continue;   // 只有一类时不画小标题
                for (const id of new Set(ids)) {
                  const real = isBundled(id);
                  if ((wantBundled && !real) || (wantExternal && real)) {
                    wrongGroup.push(id + ' 落在了「' + head + '」');
                  }
                }
              }
              return {
                ok: uniq.length > 0 && bad.length === 0 && wrongGroup.length === 0,
                detail: '列了 ' + uniq.length + ' 个插件；小标题=' + JSON.stringify(heads)
                        + (bad.length ? '  ⚠️ 没在跑却列出来了：' + bad.join(',') : '')
                        + (wrongGroup.length ? '  ⚠️ 分组归属错：' + wrongGroup.join(',') : '')
              };
            })()""", aw=True)

        check("A3 分组默认收起，且箭头可展开", """(() => {
              const group = document.getElementById('nav-plugin-group');
              const btn = document.getElementById('nav-plugin-toggle');
              const box = document.getElementById('nav-plugin-items');
              if (!group || !btn || !box) return { ok: false, detail: '分组元素缺失' };
              const hiddenFirst = box.classList.contains('hidden');
              btn.click();
              const shownAfter = !box.classList.contains('hidden');
              const aria = btn.getAttribute('aria-expanded');
              btn.click();   // 收回去，别影响后续判据
              const hiddenAgain = box.classList.contains('hidden');
              return {
                ok: hiddenFirst && shownAfter && hiddenAgain && aria === 'true',
                detail: '初始隐藏=' + hiddenFirst + ' 点击展开=' + shownAfter
                        + ' aria=' + aria + ' 再点收起=' + hiddenAgain
              };
            })()""")

        # 🔴🔴 **A5 给 A4 造反例**（2026-10-10）。没有它，A4 是一句**愿望**。
        #
        # 为什么必须造：本机此刻 6 个插件**全部 `state=loaded`、且全部是自带插件**
        # ⇒ A4 守的两个风险（"列了没跑起来的"、"外部插件混进自带栏"）
        #   **在当前数据下不可能发生**。实测：我把 `isBundled` 注入成恒 `true`、
        #   把 `p.state === "loaded"` 整个删掉，A4 **两次都照样全绿**。
        #   ⚠️ 那是**正确结论**（注入没有可观察差异），不是判据写错 ——
        #      但这意味着**判据没被验证过**，等于没写。
        #   （同一个坑本项目踩过：`meme_manager` 那次 A1 全绿，bug 却真的在。）
        #
        # 做法：在浏览器里**合成**两条假插件塞进 `/api/v1/plugins` 的响应：
        #   ① `state=failed` 且带 pages → **不该被列出来**；
        #   ② `module_path` 带命名空间前缀（=外部插件）且带页面 → 该出现在「外部插件」栏。
        #   然后重新渲染侧边栏，断言 ① 不在、② 在且落对了组。
        check("A5 合成反例：没跑起来的插件不该被列 + 外部分组归属正确", """(async () => {
              // ① 装两条假数据（只改这一条 URL 的响应）
              window.__npFake = [
                { id: 'zz_fake_failed_plugin', module_path: 'zz_fake_failed_plugin.main',
                  name: 'zz_fake_failed_plugin', display_name: '假·没跑起来', version: '0',
                  state: 'failed', error: 'boom',
                  pages: [{ name: 'p1', title: '不该出现的页面' }] },
                { id: 'zz_fake_external_plugin', module_path: 'data.plugins.zz_fake_external_plugin.main',
                  name: 'zz_fake_external_plugin', display_name: '假·外部插件', version: '0',
                  state: 'loaded', error: '',
                  pages: [{ name: 'p1', title: '外部页面' }] }
              ];
              // ② 触发重渲染（侧边栏的取数走 fetch，钩子会掺进假数据）
              const r = await fetch('/api/v1/plugins', { cache: 'no-store' });
              const d = await r.json();
              if (typeof renderNavPluginPages === 'function') renderNavPluginPages(d);
              await new Promise(res => setTimeout(res, 500));

              const items = [...document.querySelectorAll('#nav-plugin-items .nav-subitem')];
              const ids = items.map(a => (a.dataset.navPage || '').split('/')[0]);
              const failedListed = ids.includes('zz_fake_failed_plugin');
              const extListed = ids.includes('zz_fake_external_plugin');

              // ③ 外部插件必须落在「外部插件」小标题下面
              let cur = null, extGroup = null;
              for (const el of document.querySelectorAll('#nav-plugin-items > *')) {
                if (el.classList.contains('nav-subhead')) cur = (el.textContent || '').trim();
                else if (el.dataset && el.dataset.navPage
                         && el.dataset.navPage.split('/')[0] === 'zz_fake_external_plugin') {
                  extGroup = cur;
                }
              }

              // ④ 收尾：撤掉假数据，恢复真实渲染（⚠️ 判据改的是浏览器内存，磁盘没动）
              window.__npFake = null;
              const r2 = await fetch('/api/v1/plugins', { cache: 'no-store' });
              const d2 = await r2.json();
              if (typeof renderNavPluginPages === 'function') renderNavPluginPages(d2);
              await new Promise(res => setTimeout(res, 300));

              const ok = !failedListed && extListed
                         && extGroup !== null && extGroup.includes('外部插件');
              return {
                ok: ok,
                detail: '没跑起来的被列出=' + failedListed + '（应为 false）；'
                        + '外部插件被列出=' + extListed + '（应为 true）；'
                        + '它落在「' + extGroup + '」'
              };
            })()""", aw=True)

        print("\n================ B 点击能进那个插件页面 ================")

        check("B1 点子项真的打开了对应插件页（iframe 且 url 对）", """(async () => {
              const items = [...document.querySelectorAll('#nav-plugin-items .nav-subitem')];
              if (!items.length) return { ok: false, detail: '没有子项' };
              const want = items[0].dataset.navPage;       // "<插件id>/<页面名>"
              items[0].click();
              for (let i = 0; i < 80; i++) {
                await new Promise(r => setTimeout(r, 120));
                const wrap = document.getElementById('plugin-frame-wrap');
                if (wrap && !wrap.classList.contains('hidden')) break;
              }
              const wrap = document.getElementById('plugin-frame-wrap');
              const frame = document.getElementById('plugin-frame');
              const title = (document.getElementById('plugin-frame-title')||{}).textContent || '';
              const src = (frame && frame.getAttribute('src')) || '';
              const visible = wrap && !wrap.classList.contains('hidden');
              const id = want.split('/')[0];
              return {
                ok: !!visible && src.includes(encodeURIComponent(id)),
                detail: '点 ' + want + ' → 视图可见=' + visible
                        + ' 标题=' + JSON.stringify(title.slice(0, 30))
                        + ' src 含插件id=' + src.includes(encodeURIComponent(id))
              };
            })()""", aw=True)

        check("B2 子项选中态跟上（且「插件」父项也点亮）", """(() => {
              const on = [...document.querySelectorAll('.nav-subitem.active')];
              const parent = document.querySelector('.nav-item[data-nav="plugins"]');
              return {
                ok: on.length === 1 && !!parent && parent.classList.contains('active'),
                detail: '选中子项 ' + on.length + ' 条，父项点亮=' + !!(parent && parent.classList.contains('active'))
              };
            })()""")

        print("\n================ C 收起态不能变成一列没辨识度的图标 ================")

        check("C1 侧边栏收起时整块隐藏", """(async () => {
              const app = document.querySelector('.app');
              const group = document.getElementById('nav-plugin-group');
              if (!app || !group) return { ok: false, detail: '元素缺失' };
              const before = getComputedStyle(group).display;
              app.classList.add('collapsed');
              await new Promise(r => setTimeout(r, 400));
              const after = getComputedStyle(group).display;
              app.classList.remove('collapsed');   // 还原
              await new Promise(r => setTimeout(r, 400));
              return {
                ok: before !== 'none' && after === 'none',
                detail: '展开态 display=' + before + ' → 收起态 display=' + after
              };
            })()""", aw=True)

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
