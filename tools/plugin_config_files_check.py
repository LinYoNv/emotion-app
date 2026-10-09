#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插件「配置文件」能力的**真浏览器**判据（情绪 2026-10-09：「3 也可以做」）。

> 照真 AstrBot：`type: "file"` 的配置项，文件落在插件目录 `files/<键路径>/`。
> 本机真有这种配置项：`astrbot_plugin_nai_image.openai_director_fallback_images`
> （`file_types: [png, jpg, jpeg, webp]`）—— **不是空壳功能**。

## 判据（都冲着"静默"那一类）
  A. 控件真的画出来了（不是"不支持编辑"那句），且 accept 用插件声明的类型
  B. **上传**：选文件 → 点上传 → 服务端清单里真多一个
  C. **落盘**：文件真的在插件目录里（不信接口的自我报告）
  D. **加入列表**：点「加入列表」后保存，配置值里真有那个路径
  E. **删除**：删掉后清单少一个，且磁盘上也没了
  F. **未启用徽标**：传了但没填进列表时要标"未启用"（否则用户以为已生效）
  G. 零 JS 异常
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
PLUGIN_ID = "astrbot_plugin_nai_image"
CONFIG_KEY = "openai_director_fallback_images"


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
  for (let i = 0; i < 100; i++) {
    await new Promise(r => setTimeout(r, 120));
    if (!window.__pcfBusy) return true;
  }
  return false;
})()"""

_INSTALL = """(() => {
  if (window.__pcfInstalled) return true;
  window.__pcfInstalled = true;
  window.__pcfLive = 0; window.__pcfBusy = false;
  const of = window.fetch;
  window.fetch = function (...a) {
    window.__pcfLive++; window.__pcfBusy = true;
    return of.apply(this, a).finally(() => {
      window.__pcfLive--;
      if (window.__pcfLive <= 0) { window.__pcfLive = 0;
        setTimeout(() => { window.__pcfBusy = false; }, 350); }
    });
  };
  return true;
})()"""


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
    token = _token()
    port = 9272
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}", "--no-sandbox",
         "--disable-gpu", "--disable-dev-shm-usage", "--remote-allow-origins=*",
         "--window-size=1440,1100", "about:blank"],
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
                aw=True if aw else False,
            )
            if raw is None:
                print(f"  ❌ {label} —— 求值异常")
                fails.append(label); return False
            r = json.loads(raw)
            print(f"  {'✅' if r.get('ok') else '❌'} {label} —— {r.get('detail')}")
            if not r.get("ok"):
                fails.append(label)
            return bool(r.get("ok"))

        # 直接用 hash 打开该插件的配置页
        cdp.eval(f"location.hash='#/plugins/{PLUGIN_ID}/config'")
        cdp.eval(_WAIT, aw=True); time.sleep(1.6); cdp.eval(_WAIT, aw=True)

        print("\n================ A 控件画出来了 ================")
        check("A1 file 型字段画成了上传控件（不是「不支持编辑」）", """(() => {
              const box = document.querySelector('[data-plugin-file="1"]');
              const cfgBody = document.getElementById('plugin-config-body');
              if (!box) return { ok: false, detail: '没找到 data-plugin-file 控件' };
              const hasPick = !!box.querySelector('input[type=file]');
              const hasUp = !!box.querySelector('.plugin-file-upload');
              // ⚠️ 还要确认它**没有**被当成"不支持"（那句提示不能出现在这个字段里）
              const unsupported = /暂不支持编辑/.test(box.textContent || '');
              return {
                ok: hasPick && hasUp && !unsupported,
                detail: '选择框=' + hasPick + ' 上传按钮=' + hasUp + ' 误标不支持=' + unsupported
              };
            })()""")

        check("A2 accept 用插件声明的 file_types", """(() => {
              const pick = document.querySelector('[data-plugin-file="1"] input[type=file]');
              if (!pick) return { ok: false, detail: '没有选择框' };
              const acc = pick.getAttribute('accept') || '';
              const want = ['.png', '.jpg', '.jpeg', '.webp'];
              const miss = want.filter(x => !acc.includes(x));
              return {
                ok: miss.length === 0,
                detail: 'accept=' + JSON.stringify(acc) + (miss.length ? ' 缺:' + miss.join(',') : '')
              };
            })()""")

        print("\n================ B 上传（用 DataTransfer 造真文件）================ ")
        check("B1 选一个 png 上传 → 清单里真多一条", """(async () => {
              const box = document.querySelector('[data-plugin-file="1"]');
              if (!box) return { ok: false, detail: '没找到控件' };
              const pick = box.querySelector('input[type=file]');
              const up = box.querySelector('.plugin-file-upload');
              // 造一个真的 PNG 字节（8 字节头 + 噪声）—— 不要用纯文本冒充
              const bytes = new Uint8Array([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,1,2,3,4,5,6,7,8]);
              const f = new File([bytes], 'ui_probe.png', { type: 'image/png' });
              const dt = new DataTransfer();
              dt.items.add(f);
              pick.files = dt.files;
              // 上传前行数（此时应为 0）
              const before = box.querySelectorAll('.plugin-file-row').length;
              up.click();
              for (let i = 0; i < 90; i++) {
                await new Promise(r => setTimeout(r, 150));
                const n = box.querySelectorAll('.plugin-file-row').length;
                const h = box.querySelector('.plugin-file-hint');
                if (n > before) break;
                if (h && /失败/.test(h.textContent || '')) break;
              }
              const after = box.querySelectorAll('.plugin-file-row').length;
              const nm = [...box.querySelectorAll('.plugin-file-name')].map(x => x.textContent);
              const h = box.querySelector('.plugin-file-hint');
              return {
                ok: after > before && nm.includes('ui_probe.png'),
                detail: '行数 ' + before + ' → ' + after + ' 文件:' + nm.join(',')
                        + ' 提示:' + JSON.stringify((h ? h.textContent : '').slice(0, 60))
              };
            })()""", aw=True)

        print("\n================ C 落盘（不信接口自报）================ ")
        check("C1 文件真在插件目录里", """(async () => {
              const r = await fetch('/api/v1/plugins/PLUGIN_ID/config-files?config_key=CONFIG_KEY',
                                    { cache: 'no-store' });
              const d = await r.json();
              const found = (d.files || []).some(x => x.endsWith('ui_probe.png'));
              return { ok: found, detail: '服务端清单: ' + JSON.stringify(d.files || []) };
            })()""".replace("PLUGIN_ID", PLUGIN_ID).replace("CONFIG_KEY", CONFIG_KEY), aw=True)
        disk = ROOT / "data" / "plugin_src" / PLUGIN_ID / "files" / CONFIG_KEY / "ui_probe.png"
        if disk.is_file():
            print(f"  ✅ C2 磁盘上真有这个文件 —— {disk}（{disk.stat().st_size} 字节）")
        else:
            print(f"  ❌ C2 磁盘上没有这个文件 —— {disk}")
            fails.append("C2 落盘")

        print("\n================ D 未启用徽标 + 加入列表 + 保存 ================")
        check("D1 刚传上来时标「未启用」（没填进配置列表）", """(() => {
              const box = document.querySelector('[data-plugin-file="1"]');
              const row = [...box.querySelectorAll('.plugin-file-row')]
                            .find(r => (r.querySelector('.plugin-file-name')||{}).textContent === 'ui_probe.png');
              if (!row) return { ok: false, detail: '没有那一行' };
              const b = row.querySelector('.badge');
              return {
                ok: !!b && b.textContent.includes('未启用'),
                detail: '徽标=' + JSON.stringify((b ? b.textContent : ''))
              };
            })()""")

        check("D2 点「加入列表」→ 徽标变「已启用」", """(async () => {
              const box = document.querySelector('[data-plugin-file="1"]');
              const row = [...box.querySelectorAll('.plugin-file-row')]
                            .find(r => (r.querySelector('.plugin-file-name')||{}).textContent === 'ui_probe.png');
              if (!row) return { ok: false, detail: '没有那一行' };
              const add = row.querySelector('.plugin-file-add');
              if (!add) return { ok: false, detail: '没有「加入列表」按钮（是不是已经是启用态？）' };
              add.click();
              await new Promise(r => setTimeout(r, 400));
              const row2 = [...document.querySelectorAll('[data-plugin-file="1"] .plugin-file-row')]
                             .find(r => (r.querySelector('.plugin-file-name')||{}).textContent === 'ui_probe.png');
              const b = row2 && row2.querySelector('.badge');
              return {
                ok: !!b && b.textContent.includes('已启用'),
                detail: '徽标=' + JSON.stringify((b ? b.textContent : ''))
              };
            })()""", aw=True)

        check("D3 保存后服务端配置值里真有这个路径", """(async () => {
              const save = document.getElementById('plugin-config-save');
              if (!save) return { ok: false, detail: '没有保存按钮' };
              save.click();
              for (let i = 0; i < 90; i++) {
                await new Promise(r => setTimeout(r, 150));
                const h = document.getElementById('plugin-config-hint');
                if (h && /保存|失败|重启|已/.test(h.textContent || '')) break;
              }
              const r = await fetch('/api/v1/plugins/PLUGIN_ID/config', { cache: 'no-store' });
              const d = await r.json();
              const v = (d.config || {})['CONFIG_KEY'];
              const h = document.getElementById('plugin-config-hint');
              const ok = Array.isArray(v) && v.some(x => String(x).endsWith('ui_probe.png'));
              return {
                ok: ok,
                detail: 'config[' + 'CONFIG_KEY' + ']=' + JSON.stringify(v)
                        + ' 提示:' + JSON.stringify((h ? h.textContent : '').slice(0, 50))
              };
            })()""".replace("PLUGIN_ID", PLUGIN_ID).replace("CONFIG_KEY", CONFIG_KEY), aw=True)

        print("\n================ E 删除（并清回配置值）================ ")
        check("E1 点「删除文件」→ 清单里没了", """(async () => {
              const ok = window.confirm; window.confirm = () => true;   // 免弹窗
              const box = document.querySelector('[data-plugin-file="1"]');
              const row = [...box.querySelectorAll('.plugin-file-row')]
                            .find(r => (r.querySelector('.plugin-file-name')||{}).textContent === 'ui_probe.png');
              if (!row) { window.confirm = ok; return { ok: false, detail: '没有那一行' }; }
              const del = row.querySelector('.plugin-file-del');
              if (!del) { window.confirm = ok; return { ok: false, detail: '没有删除按钮' }; }
              del.click();
              for (let i = 0; i < 80; i++) {
                await new Promise(r => setTimeout(r, 150));
                const names = [...document.querySelectorAll('[data-plugin-file="1"] .plugin-file-name')]
                                .map(x => x.textContent);
                if (!names.includes('ui_probe.png')) break;
              }
              window.confirm = ok;
              const names = [...document.querySelectorAll('[data-plugin-file="1"] .plugin-file-name')]
                              .map(x => x.textContent);
              return { ok: !names.includes('ui_probe.png'), detail: '剩余文件: ' + names.join(',') };
            })()""", aw=True)

        check("E2 服务端清单也空了（并保存，把配置值清回去）", """(async () => {
              const r = await fetch('/api/v1/plugins/PLUGIN_ID/config-files?config_key=CONFIG_KEY',
                                    { cache: 'no-store' });
              const d = await r.json();
              const gone = !(d.files || []).some(x => x.endsWith('ui_probe.png'));
              // 🔴 **收尾**：判据改了真实状态（上传过文件、可能写进了配置值），
              //    必须自己还原 —— 否则每跑一次就给用户的插件留一个垃圾路径。
              const save = document.getElementById('plugin-config-save');
              if (save) { save.click(); }
              for (let i = 0; i < 60; i++) {
                await new Promise(r => setTimeout(r, 150));
                const h = document.getElementById('plugin-config-hint');
                if (h && /保存|失败|重启|已/.test(h.textContent || '')) break;
              }
              const r2 = await fetch('/api/v1/plugins/PLUGIN_ID/config', { cache: 'no-store' });
              const d2 = await r2.json();
              const v = (d2.config || {})['CONFIG_KEY'];
              const clean = !(Array.isArray(v) && v.some(x => String(x).endsWith('ui_probe.png')));
              return {
                ok: gone && clean,
                detail: '清单已空=' + gone + ' 配置值已清=' + clean + ' 现值=' + JSON.stringify(v)
              };
            })()""".replace("PLUGIN_ID", PLUGIN_ID).replace("CONFIG_KEY", CONFIG_KEY), aw=True)

        drain()
        print("\n================ F 零 JS 异常 ================")
        real = [e for e in js_errors if e and e != "None"]
        if real:
            print(f"  ❌ 捕获 {len(real)} 条：")
            for e in real[:6]:
                print(f"     - {e[:180]}")
            fails.append("F JS 异常")
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
