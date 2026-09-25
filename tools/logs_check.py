#!/usr/bin/env python3
"""「日志」页判据（真浏览器，Chrome CDP）。

为什么需要它（curl/单测都覆盖不到的部分）：
    * `curl` 能验 `/api/v1/logs` 返回什么，**验不了 JS 有没有把它画出来**；
    * 单测能验路由，**验不了"自动刷新到底有没有在跑、离开页面有没有停"**
      —— 那正是本页最容易出的两个真 bug：
        ① 定时器离开页面不停 → 用户看不见却仍在发请求（白刷 + 隐私上也不该）；
        ② 读失败被画成空列表 → 用户以为"服务很干净"（最误导人的失败）。

用法:
    python3 tools/logs_check.py [BASE_URL] [TOKEN]

退出码:0 = 全绿;非 0 = 有判据失败（**没有"跳过"** —— 前置不满足即失败）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_check import CHROME, PORT, CDP  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6190/"
ROUTE = "#/logs"


def read_token() -> str:
    """从 .env 读访问令牌（**必须去掉单引号** —— 踩过：带引号会被当成错误令牌）。"""
    env = Path(__file__).resolve().parent.parent / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("EMOTION_AUTH_ACCESS_TOKEN"):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("❌ .env 里没有 EMOTION_AUTH_ACCESS_TOKEN")


def main() -> int:
    token = sys.argv[2] if len(sys.argv) > 2 else read_token()
    failures: list[str] = []
    checks = 0

    def check(label: str, ok: bool, detail: str = "") -> bool:
        nonlocal checks
        checks += 1
        print(f"  {'✅' if ok else '❌'} {label}" + (f" — {detail}" if (detail and not ok) else ""))
        if not ok:
            failures.append(label)
        return bool(ok)

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--remote-allow-origins=*", "--window-size=1200,1800", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(48):
            time.sleep(0.25)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=3) as r:
                    pages = json.loads(r.read())
                target = next((t for t in pages if t["type"] == "page"), None)
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
        # 录音机要在**页面脚本之前**装（否则 boot() 早请求完了，读到 0 次 = 假红）
        cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            source=(
                "window.__rec=[]; window.__jsErrors=[]; window.__timers=0;"
                "window.addEventListener('error',function(e){window.__jsErrors.push(String(e.message));});"
                "(function(){var o=window.fetch; window.fetch=function(i,init){"
                "  var u=(typeof i==='string')?i:(i&&i.url)||'';"
                "  if(u.indexOf('/api/v1/logs')===0) window.__rec.push(u);"
                "  return o.apply(this,arguments);};})();"
                # 🔴 只追踪**日志页自己的**定时器（`logsTimer` 的回调是 loadLogs）——
                #    不能数"净 setInterval 数量"：页面本来就有两个长期定时器
                #    （状态刷新 30s、登录复查 30s），数总量会得到恒 ≥2 的假红。
                #    实测踩过：我第一版数总净数 → ⑧ 判据报 "timers=2"。
                #    判据要盯**被测对象**，不是环境噪声。
                "(function(){var si=window.setInterval,ci=window.clearInterval;"
                " window.__logTimerId=0; window.__logTimerLive=false;"
                " window.setInterval=function(fn,ms){var id=si.apply(this,arguments);"
                "   try{if(String(fn).indexOf('loadLogs')>=0){window.__logTimerId=id;window.__logTimerLive=true;}}catch(e){}"
                "   return id;};"
                " window.clearInterval=function(id){"
                "   if(id&&id===window.__logTimerId){window.__logTimerLive=false;}"
                "   return ci.apply(this,arguments);};})();"
            ),
        )

        cdp.send("Page.navigate", url=BASE)
        time.sleep(3.0)
        cdp.eval(
            "(function(){var t=document.getElementById('login-token');"
            " t.value=" + json.dumps(token) + ";"
            " document.getElementById('login-form').dispatchEvent("
            " new Event('submit',{cancelable:true})); return 'ok';})()"
        )
        time.sleep(2.5)
        cdp.eval(f"location.hash = {json.dumps(ROUTE)}; 'ok'")
        time.sleep(0.5)
        cdp.send("Page.reload")          # 真刷新，确保走 boot()
        time.sleep(4.0)

        print("\n========== 第一部分：页面结构 ==========")

        # ---- ① 导航项与面板都在 ----
        nav = cdp.eval(
            "(function(){var a=document.querySelector('.nav-item[data-nav=\"logs\"]');"
            " var p=document.getElementById('tab-logs');"
            " return {nav:!!a, panel:!!p, label:a?a.innerText.trim():'',"
            "   list:!!document.getElementById('logs-list'),"
            "   hint:!!document.getElementById('logs-hint')};})()"
        )
        check("① 侧边栏有「日志」入口", bool(nav["nav"]), str(nav))
        check("① 日志面板存在（#tab-logs）", bool(nav["panel"]), str(nav))
        check("① 日志列表容器存在（#logs-list）", bool(nav["list"]), str(nav))
        check("① 提示区存在（#logs-hint）", bool(nav["hint"]), str(nav))

        # ---- ② 点导航真的能切过去 ----
        cdp.eval("(function(){var a=document.querySelector('.nav-item[data-nav=\"logs\"]');"
                 " a.click(); return 'ok';})()")
        time.sleep(2.0)
        shown = cdp.eval(
            "(function(){var p=document.getElementById('tab-logs');"
            " return {hidden:p.classList.contains('hidden'),"
            "   active:!!document.querySelector('.nav-item[data-nav=\"logs\"].active')};})()"
        )
        check("② 点导航后日志面板**显示**（不是还藏着）", shown["hidden"] is False, str(shown))
        check("② 导航项被高亮（active）", bool(shown["active"]), str(shown))

        print("\n========== 第二部分：真的取到并画出了日志 ==========")

        # ---- ③ 真的请求了后端 ----
        rec = cdp.eval("window.__rec || []")
        check("③ 页面确实请求了 /api/v1/logs",
              any("/api/v1/logs" in u for u in rec), f"记录={rec}")

        # ---- ④ 提示区说的是"共 N 条"，不是"读取失败" ----
        hint = cdp.eval("(document.getElementById('logs-hint')||{}).innerText || ''")
        check("④ 提示区报告了条数（不是读取失败）",
              ("共" in hint and "条" in hint) or "没有符合条件" in hint, f"hint={hint!r}")

        # ---- ⑤ 真的画出了行（不是空容器）----
        rendered = cdp.eval(
            "(function(){var rows=document.querySelectorAll('#logs-list .log-row');"
            " var first=rows[0];"
            " return {n:rows.length,"
            "   level:first?first.querySelector('.log-level').innerText.trim():'',"
            "   ts:first?first.querySelector('.log-ts').innerText.trim():'',"
            "   msg:first?first.querySelector('.log-msg').innerText.trim():''};})()"
        )
        check("⑤ 日志行真的渲染出来了（>0 行）", rendered["n"] > 0, str(rendered))
        check("⑤ 每行有级别文字（不只靠颜色）", bool(rendered["level"]), str(rendered))
        check("⑤ 每行有时间戳", bool(rendered["ts"]), str(rendered))
        check("⑤ 每行有消息文本", bool(rendered["msg"]), str(rendered))

        print("\n========== 第三部分：级别过滤真的生效 ==========")

        # ---- ⑥ 选 ERROR 后，画出来的**全是** ERROR（不是只发请求不重画）----
        cdp.eval("(function(){var s=document.getElementById('logs-level');"
                 " s.value='ERROR'; s.dispatchEvent(new Event('change')); return 'ok';})()")
        time.sleep(2.0)
        lv = cdp.eval(
            "(function(){var rows=document.querySelectorAll('#logs-list .log-row');"
            " var got=[].map.call(rows,function(r){"
            "   return r.querySelector('.log-level').innerText.trim();});"
            " return {levels:got, n:rows.length};})()"
        )
        bad = [x for x in lv["levels"] if x not in ("ERROR", "CRITICAL")]
        check("⑥ 选「ERROR 及以上」后**画出来的**只剩 ERROR/CRITICAL",
              not bad, f"混进了 {bad}（共 {lv['n']} 行）")

        # 切回全部，别把状态留给后面的判据
        cdp.eval("(function(){var s=document.getElementById('logs-level');"
                 " s.value=''; s.dispatchEvent(new Event('change')); return 'ok';})()")
        time.sleep(1.5)

        print("\n========== 第四部分：自动刷新（进开 / 离停） ==========")

        # ---- ⑦ 在日志页时定时器是开着的 ----
        t_on = cdp.eval("!!window.__logTimerLive")
        check("⑦ 停在日志页时自动刷新定时器在跑", bool(t_on),
              "没看到日志页的定时器 —— 自动刷新没开起来")

        # ---- ⑧ 离开日志页 → 定时器必须停 ----
        cdp.eval("location.hash = '#/settings'; 'ok'")
        time.sleep(1.5)
        t_off = cdp.eval("!!window.__logTimerLive")
        check("⑧ **离开日志页后定时器停了**", t_off is False,
              "定时器还在跑 —— 用户看不见却仍在发请求")

        # ---- ⑨ 自动刷新真的会产生新请求（不是只挂了个表）----
        cdp.eval("location.hash = '#/logs'; 'ok'")
        time.sleep(2.0)
        cdp.eval("window.__rec = []; 'ok'")
        time.sleep(7.0)      # 自动刷新间隔 5s，留足余量
        rec2 = cdp.eval("window.__rec || []")
        check("⑨ 自动刷新真的又发了请求（5 秒内至少 1 次）",
              len(rec2) >= 1, f"记录={rec2}")

        # ---- ⑩ 取消勾选后不再发请求 ----
        cdp.eval("(function(){var a=document.getElementById('logs-auto');"
                 " a.checked=false; a.dispatchEvent(new Event('change')); return 'ok';})()")
        time.sleep(1.0)
        cdp.eval("window.__rec = []; 'ok'")
        time.sleep(7.0)
        rec3 = cdp.eval("window.__rec || []")
        check("⑩ 取消「自动刷新」后**不再发请求**", len(rec3) == 0,
              f"仍然发了 {len(rec3)} 次 —— 开关是假的")

        print("\n========== 第五部分：失败必须响亮 ==========")

        # ---- ⑪ 后端 500 时**提示区说清原因**，且不假装"没有日志" ----
        cdp.eval(
            "(function(){var o=window.fetch;"
            " window.fetch=function(i,init){"
            "   var u=(typeof i==='string')?i:(i&&i.url)||'';"
            "   if(u.indexOf('/api/v1/logs')===0){"
            "     return Promise.resolve(new Response("
            "       JSON.stringify({error:{message:'模拟读取失败'}}),"
            "       {status:500, headers:{'Content-Type':'application/json'}}));}"
            "   return o.apply(this,arguments);}; return 'ok';})()"
        )
        cdp.eval("(function(){document.getElementById('logs-refresh').click(); return 'ok';})()")
        time.sleep(2.0)
        hint2 = cdp.eval("(document.getElementById('logs-hint')||{}).innerText || ''")
        check("⑪ 读失败时提示区**说清是失败**（不是空列表装没事）",
              "失败" in hint2, f"hint={hint2!r}")

        # ---- ⑫ 全程没有未捕获 JS 异常 ----
        errs = cdp.eval("window.__jsErrors || []")
        check("⑫ 全程没有未捕获的 JS 异常", not errs, f"errors={errs}")

        print("\n" + "=" * 68)
        if failures:
            print(f"❌ {len(failures)}/{checks} 条判据失败：")
            for f in failures:
                print(f"   - {f}")
            return 1
        print(f"✅ 全部 {checks} 条判据通过")
        return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()


if __name__ == "__main__":
    sys.exit(main())
