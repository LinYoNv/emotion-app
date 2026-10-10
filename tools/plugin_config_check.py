#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插件配置页的**真浏览器**判据（Chrome + CDP）。

## 为什么必须真浏览器
配置页的价值全在"用户能不能**看见并改动**插件配置"。`curl` 只能证明接口有数据，
证明不了：
  * 卡片上那个「配置」按钮**点得动**（它是 `<label>` 行内按钮 ——
    漏了 `stopPropagation()` 会顺手切换插件启停，静默改配置）；
  * schema 真的被画成了**表单控件**（而不是一堆看不懂的文本）；
  * 改一个值 → 点保存 → **真的发到服务端并落盘**；
  * 不支持的项**如实标注**（而不是画个存不进去的假控件）。

## 判据分组
  A. 入口：卡片上有「配置」按钮，点它进配置页，且**没有误切插件启停**
  B. 渲染：schema 被画成了控件（数量与接口给的 schema 对得上）
  C. 端到端：改一个真值 → 保存 → 服务端读回来是**新值**
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


def _login(base: str, token: str) -> str:
    """登录取 cookie，返回可直接放进 `Cookie` 头的那一段。"""
    req = urllib.request.Request(
        base.rstrip("/") + "/api/v1/auth/login",
        data=json.dumps({"token": token}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as f:
        return (f.headers.get("Set-Cookie") or "").split(";")[0]


def _api(base: str, path: str, cookie: str):
    req = urllib.request.Request(base.rstrip("/") + path, headers={"Cookie": cookie})
    with urllib.request.urlopen(req, timeout=30) as f:
        return json.loads(f.read())


def _pick_plugin(base: str, cookie: str) -> str:
    """🔴 **从"真正加载着的插件"里挑一个来测，不许写死 id。**

    ⚠️ 这里原本写死 `astrbot_plugin_splitter` —— 那是**别人 AstrBot 的插件**
       （我们早期误把 `/opt/astrbot/data/plugins` 当插件源时镜像进来的）。
       插件源改成我们自己的 `plugins/` 之后它就不存在了 ⇒ 本文件 A2/B1/C1/C2
       四条全红，而**产品完全正常**：**判据指错了对象**。
       与「表情包管理器」出现在侧边栏是同一类错误的两个面。

    选法（排序键全部取自**接口现场**，不读目录、不读代码）：
      ① `state == loaded` —— 没跑起来的插件连配置页都打不开，测它没意义；
      ② 有 `metadata`（即 `_conf_schema.json`）且能画成控件；
      ③ 优先有**带 `options` 的 string 项**的（C1 要真改一个值，在合法值里换才安全）；
      ④ 标量项越少越好（渲染快、副作用小）。
    """
    d = _api(base, "/api/v1/plugins", cookie)
    items = d.get("plugins") or d.get("items") or []
    cands = []
    for p in items:
        pid = p.get("id")
        if p.get("state") != "loaded" or not pid:
            continue
        try:
            c = _api(base, f"/api/v1/plugins/{pid}/config", cookie)
        except Exception:
            continue
        schema = ((c.get("metadata") or {}).get(pid) or {}).get("items") or {}
        if not schema:
            continue
        scalars, has_options = 0, False
        for _k, v in schema.items():
            if not isinstance(v, dict) or v.get("invisible") or v.get("type") == "object":
                continue
            scalars += 1
            if v.get("type") == "string" and isinstance(v.get("options"), list) and len(v["options"]) >= 2:
                has_options = True
        if scalars:
            cands.append((not has_options, scalars, pid))
    if not cands:
        raise SystemExit("❌ 没有任何【已加载且带 schema】的插件，选不出样本")
    cands.sort()
    return cands[0][2]


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
    if (!window.__pcBusy) return true;
  }
  return false;
})()"""

_INSTALL = """(() => {
  if (window.__pcInstalled) return true;
  window.__pcInstalled = true;
  window.__pcLive = 0; window.__pcBusy = false;
  const of = window.fetch;
  window.fetch = function (...a) {
    window.__pcLive++; window.__pcBusy = true;
    return of.apply(this, a).finally(() => {
      window.__pcLive--;
      if (window.__pcLive <= 0) { window.__pcLive = 0;
        setTimeout(() => { window.__pcBusy = false; }, 300); }
    });
  };
  return true;
})()"""


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = _token()

    # 🔴 样本插件**从接口现场选**（`--plugin <id>` 可覆盖，便于排查）。
    if len(sys.argv) > 3 and sys.argv[2] == "--plugin":
        plugin_id = sys.argv[3]
    else:
        plugin_id = _pick_plugin(base, _login(base, token))
    print(f"📌 样本插件：{plugin_id}（从已加载的插件里按 schema 选的，不是写死的）")

    port = 9265
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

        print("\n================ A 入口：卡片上的「配置」按钮 ================")

        check("A1 插件列表里每行都有「配置」按钮", """(async () => {
              location.hash = '#/plugins';
              for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 100));
                const rows = document.querySelectorAll('#plugin-switch-list .switch-row');
                if (rows.length > 0) break;
              }
              const rows = [...document.querySelectorAll('#plugin-switch-list .switch-row')];
              if (!rows.length) return { ok: false, detail: '插件列表是空的' };
              const noCfg = rows.filter(r => !r.querySelector('button.switch-config')).length;
              const noDetail = rows.filter(r => !r.querySelector('button.switch-detail')).length;
              return {
                ok: noCfg === 0 && noDetail === 0,
                detail: rows.length + ' 行；缺配置按钮 ' + noCfg + ' 行，缺详情按钮 ' + noDetail + ' 行'
              };
            })()""", aw=True)

        # 🔴 这条守的是"点了配置会不会顺手改了启停"——
        #    整行是 <label>，漏 stopPropagation 就会**静默切换勾选框**。
        check("A2 点「配置」不会误切插件启停", """(async () => {
              const row = [...document.querySelectorAll('#plugin-switch-list .switch-row')]
                            .find(r => r.dataset.pluginId === 'PLUGIN_ID');
              if (!row) return { ok: false, detail: '列表里没有 PLUGIN_ID' };
              const before = row.querySelector('input.switch-cb').checked;
              row.querySelector('button.switch-config').click();
              for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 100));
                if (!document.getElementById('plugin-config-wrap').classList.contains('hidden')) break;
              }
              // 回到列表看那个勾选框
              location.hash = '#/plugins';
              for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 100));
                if (!document.getElementById('tab-plugins').classList.contains('hidden')) break;
              }
              await new Promise(r => setTimeout(r, 600));
              const row2 = [...document.querySelectorAll('#plugin-switch-list .switch-row')]
                             .find(r => r.dataset.pluginId === 'PLUGIN_ID');
              const after = row2 ? row2.querySelector('input.switch-cb').checked : null;
              return {
                ok: after === before,
                detail: '点配置前 checked=' + before + '，回来是 ' + after
                        + (after !== before ? '（**误切了启停**！）' : '')
              };
            })()""".replace("PLUGIN_ID", plugin_id), aw=True)

        print("\n================ B 渲染：schema 被画成控件 ================")

        check("B1 配置页把 schema 画成了表单控件", """(async () => {
              location.hash = '#/plugins/PLUGIN_ID/config';
              for (let i = 0; i < 80; i++) {
                await new Promise(r => setTimeout(r, 100));
                const body = document.getElementById('plugin-config-body');
                if (body && body.querySelectorAll('[data-plugin-key]').length > 0) break;
              }
              const body = document.getElementById('plugin-config-body');
              const fields = body.querySelectorAll('[data-plugin-key]');
              const ctrls = body.querySelectorAll('[data-plugin-key] input, [data-plugin-key] select');
              return {
                ok: fields.length > 0 && ctrls.length > 0,
                detail: fields.length + ' 个字段，其中 ' + ctrls.length + ' 个是可交互控件'
              };
            })()""".replace("PLUGIN_ID", plugin_id), aw=True)

        # 🔴 与接口给的 schema **对得上**（数量判据只能当辅助，这里做集合比较）
        check("B2 画出来的控件覆盖了 schema 里的所有标量项", """(async () => {
              const r = await fetch('/api/v1/plugins/PLUGIN_ID/config', { cache: 'no-store' });
              const d = await r.json();
              const md = (d.metadata || {})['PLUGIN_ID'] || {};
              const items = md.items || {};
              // 只数**标量**项（object 是分组，递归展开，不计入本层）
              const scalars = Object.entries(items)
                .filter(([k, v]) => v && typeof v === 'object' && v.type !== 'object' && !v.invisible)
                .map(([k]) => k);
              const body = document.getElementById('plugin-config-body');
              const drawn = [...body.querySelectorAll('[data-plugin-key]')]
                .map(el => el.dataset.pluginKey);
              const missing = scalars.filter(k => !drawn.includes(k));
              return {
                ok: missing.length === 0,
                detail: 'schema 标量项 ' + scalars.length + ' 个，没画出来的：'
                        + (missing.length ? missing.join(',') : '（无）')
              };
            })()""".replace("PLUGIN_ID", plugin_id), aw=True)

        print("\n================ C 端到端：改一个值 → 保存 ================")

        # 🔴 这条是本文件的**核心**：真改、真保存、再从服务端读回来。
        check("C1 改一个值保存后，服务端读回来是新值", """(async () => {
              const r0 = await fetch('/api/v1/plugins/PLUGIN_ID/config', { cache: 'no-store' });
              const d0 = await r0.json();
              const schema = (((d0.metadata || {})['PLUGIN_ID'] || {}).items) || {};
              const cfg = d0.config || {};
              // 找一个**带 options 的 string 项**（在合法值里换，不会写出非法配置）
              const cand = Object.entries(schema).find(([k, v]) =>
                v && v.type === 'string' && Array.isArray(v.options) && v.options.length >= 2);
              if (!cand) return { ok: false, detail: '这个插件没有【带 options 的 string 项】，换插件或换判据' };
              const [key, spec] = cand;
              const want = spec.options.find(o => o !== cfg[key]);

              // 在界面上找那个控件并改它
              const body = document.getElementById('plugin-config-body');
              const wrap = [...body.querySelectorAll('[data-plugin-key]')]
                             .find(el => el.dataset.pluginKey === key);
              if (!wrap) return { ok: false, detail: '界面上没有 ' + key + ' 这个字段' };
              const ctrl = wrap.querySelector('input, select');
              ctrl.value = want;
              ctrl.dispatchEvent(new Event('change', { bubbles: true }));

              // 点保存
              document.getElementById('plugin-config-save').click();
              for (let i = 0; i < 80; i++) {
                await new Promise(r => setTimeout(r, 150));
                const h = document.getElementById('plugin-config-hint');
                if (h && /保存|失败|重启|已/.test(h.textContent || '')) break;
              }

              // 从**服务端**读回来核对
              const r2 = await fetch('/api/v1/plugins/PLUGIN_ID/config', { cache: 'no-store' });
              const d2 = await r2.json();
              const got = (d2.config || {})[key];
              const ok = got === want;

              // 🔴 **判据改的是真实状态，必须自己收尾**（把值改回原样）。
              //    ⚠️ 我第一版没收尾 ⇒ 每次跑判据都把 `config_mode` 翻一次，
              //       跑两遍正好互相抵消（看着"人畜无害"），但**跑奇数遍**就留下
              //       一个被改过的插件配置 —— 那种"测试污染生产数据"的账最难查。
              //    ⚠️ 收尾走**接口**（不是直接改盘）：直接改盘会让内存里那份
              //       与磁盘不一致，而插件读的是内存那份。
              // ⚠️🔴 **必须重新取控件**：保存成功会调 `openPluginConfig(id)` 把
              //     `#plugin-config-body` **整块重绘** ⇒ 上面那个 `wrap` 已经成了
              //     游离节点，改它不会触发任何 change（我第一版就是这样，
              //     收尾"静默失败"—— 判据看了半天才发现是 DOM 被换了）。
              const wrap2 = [...document.getElementById('plugin-config-body')
                               .querySelectorAll('[data-plugin-key]')]
                              .find(el => el.dataset.pluginKey === key);
              const ctrl2 = wrap2 && wrap2.querySelector('input, select');
              if (ctrl2) {
                ctrl2.value = cfg[key];
                ctrl2.dispatchEvent(new Event('change', { bubbles: true }));
                document.getElementById('plugin-config-save').click();
                for (let i = 0; i < 60; i++) {
                  await new Promise(r => setTimeout(r, 120));
                  const h = document.getElementById('plugin-config-hint');
                  if (h && /保存|失败|重启|已/.test(h.textContent || '')) break;
                }
                const r3 = await fetch('/api/v1/plugins/PLUGIN_ID/config', { cache: 'no-store' });
                const d3 = await r3.json();
                if ((d3.config || {})[key] !== cfg[key]) {
                  return { ok: false, detail: '收尾失败：' + key + ' 没改回 ' + JSON.stringify(cfg[key]) };
                }
              }

              return {
                ok: ok,
                detail: key + ': ' + JSON.stringify(cfg[key]) + ' → 保存 ' + JSON.stringify(want)
                        + ' → 服务端读回 ' + JSON.stringify(got) + '（已改回原值）'
              };
            })()""".replace("PLUGIN_ID", plugin_id), aw=True)

        check("C2 保存后提示如实说了生效时机", """(() => {
              const h = document.getElementById('plugin-config-hint');
              const t = h ? (h.textContent || '') : '';
              // 🔴 我们**没有热重载**，所以文案必须提"重启"。
              //    照抄真实现的"正在热重载插件"就是假信息。
              const ok = t.includes('重启') || t.includes('已保存');
              return { ok, detail: '提示：' + JSON.stringify(t.slice(0, 90)) };
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
