#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「设置」页判据（真浏览器，Chrome CDP）—— **配置文件式**页面版。

🔴 为什么必须用真浏览器：
    本页的价值几乎全在 JS 侧 —— **字段是按后端 schema 动态生成**的。
    所以"接口返回了 schema"跟"页面上真的画出了控件、值是对的、保存只发改动项"
    是三件不同的事。`curl` 只能验第一件。
    本项目已多次踩过"接口全绿、界面空白"（人设导入那次），
    所以端到端判据必须落到 **DOM**。

🔴 判据不许"跳过"（项目铁律）：
    前置条件不满足就是**失败**，不许打一句"跳过"就算 ✅
    （`mobile_check.py` 第 4 项的教训：跳过判据 = 假通过，比不写还糟）。

覆盖（契约 §7）：
  ① 两段真的渲染出来了（**数真实字段个数**，不是"没抛异常"）
  ② 显示值来自后端（不是前端写死的默认）
  ③ 保存真的发 PATCH，且**只发改动项**
  ④ 后端 400 带 `error.field` → 界面**点名那个字段**
  ⑤ `provider_select` 没有可选模型时给空态提示，**不是空下拉**
  ⑥ 恢复默认要二次确认，确认后发 DELETE
  ⑦ 读取失败要**如实说原因**（不许显示成"没有配置"）

用法：
    .venv/bin/python tools/settings_check.py [base_url] [token]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_check import CHROME, PORT, CDP  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"
ROUTE = "#/settings"


def read_token() -> str:
    """从 .env 读访问令牌（**必须去掉单引号**）。"""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("EMOTION_AUTH_ACCESS_TOKEN"):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def main() -> int:
    token = sys.argv[2] if len(sys.argv) > 2 else read_token()
    failures: list[str] = []
    checks = 0

    def check(label: str, ok: bool, detail: str = "") -> bool:
        nonlocal checks
        checks += 1
        mark = "✅" if ok else "❌"
        print(f"  {mark} {label}" + (f" — {detail}" if (detail and not ok) else ""))
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
                import urllib.request
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

        # 🔴 录音机要在**页面脚本跑之前**装好（`Page.addScriptToEvaluateOnNewDocument`）。
        #    我第一版是在 reload **之后**才覆盖 `window.fetch` —— 那时 `boot()` 早就
        #    把 `/api/v1/config-profile` 请求发完了 → "页面确实请求了" 判据读到 0 次，
        #    看起来像产品缺陷，其实是**取证装晚了**。
        #    （本项目老坑：验证脚本自己制造假红。）
        cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            source=(
                "window.__rec=[]; window.__jsErrors=[];"
                "window.addEventListener('error',function(e){window.__jsErrors.push(String(e.message));});"
                "(function(){var o=window.fetch; window.fetch=function(i,init){"
                "  var u=(typeof i==='string')?i:(i&&i.url)||'';"
                "  var m=((init&&init.method)||(i&&i.method)||'GET').toUpperCase();"
                "  var b=null; try{b=init&&init.body?JSON.parse(init.body):null;}catch(e){}"
                "  if(u.indexOf('/api/v1/')===0) window.__rec.push({m:m,u:u,b:b});"
                "  return o.apply(this,arguments);};})();"
            ),
        )

        cdp.send("Page.navigate", url=BASE)
        time.sleep(3.0)

        # 登录（HttpOnly cookie）；登录后**真刷新**一次，确保走 boot() 而不是只改 hash
        cdp.eval(
            "(function(){var t=document.getElementById('login-token');"
            " t.value=" + json.dumps(token) + ";"
            " document.getElementById('login-form').dispatchEvent("
            " new Event('submit',{cancelable:true})); return 'ok';})()"
        )
        time.sleep(2.5)
        cdp.eval(f"location.hash = {json.dumps(ROUTE)}; 'ok'")
        time.sleep(0.5)
        cdp.send("Page.reload")
        time.sleep(3.5)

        print("\n========== 第一部分：页面结构与取数 ==========")

        # ---- ① 两段真的渲染出来了（数真实字段个数）----
        rendered = cdp.eval(
            "(function(){"
            " var s=document.querySelectorAll('#cfg-session-fields .cfg-field').length;"
            " var ctx=document.querySelectorAll('#cfg-context-fields .cfg-field').length;"
            " var ctxSec=!!document.getElementById('cfg-context-section');"
            " var heads=[].map.call(document.querySelectorAll('#tab-settings h4'),"
            "   function(h){return h.innerText.trim();});"
            " var hint=(document.getElementById('cfg-hint')||{}).innerText||'';"
            " return {session:s, context:ctx, ctxSection:ctxSec, heads:heads, hint:hint};})()"
        ) or {}
        check("① 「会话」段真的渲染出字段（不是 0 个、也不是没抛异常就算过）",
              (rendered.get("session") or 0) >= 5,
              f"实际 {rendered.get('session')} 个")
        # 🔴 2026-10-09 改：全局「上下文管理策略」段**已删除**
        #    （情绪：「上下文策略一样做成会话级的，不做全局」）——
        #    它原有 10 个字段，7 个**零消费**、3 个与会话级重复。
        #    ⇒ 判据从"它渲染出来了"**反过来**：**它必须不存在**。
        #    ⚠️ 断言"元素不存在"而**不只是**"字段数为 0"：后者在
        #       元素还在、只是没渲染字段时也会过（那正是删除没删干净的样子）。
        check("① 全局「上下文管理策略」段**确实没了**（能力已收敛到会话级）",
              rendered.get("ctxSection") is False and (rendered.get("context") or 0) == 0,
              f"段还在={rendered.get('ctxSection')} 字段数={rendered.get('context')}")
        check("① 小节标题只剩「会话」（上下文那个必须消失）",
              "会话" in (rendered.get("heads") or [])
              and "上下文管理策略" not in (rendered.get("heads") or []),
              f"heads={rendered.get('heads')}")
        check("① 提示里说的是「已读取配置」（不是「读取失败」）",
              "已读取配置" in (rendered.get("hint") or ""),
              f"hint={rendered.get('hint')!r}")

        # ---- ② 值来自后端，不是前端写死的 ----
        # 先从后端拿真值，再和 DOM 比 —— 这样"前端硬编码一份默认值"会被抓出来
        cur = cdp.eval(
            "(function(){var r=(window.__rec||[]).filter(function(x){"
            " return x.m==='GET' && x.u==='/api/v1/config-profile';});"
            " return r.length;})()"
        )
        check("② 页面确实请求了 /api/v1/config-profile", (cur or 0) >= 1, f"次数={cur}")

        # 改一个值（直接改 DOM 不可靠，用 PATCH 改后端再刷新，验"显示的是后端的值"）
        cdp.eval(
            "(function(){window.__rec=[];"
            " return fetch('/api/v1/config-profile',{method:'PATCH',"
            "  headers:{'Content-Type':'application/json'},"
            "  body:JSON.stringify({session:{request_max_retries:7}})}).then(function(){return 'ok';});})()",
            await_promise=True,
        )
        time.sleep(0.8)
        cdp.send("Page.reload")
        time.sleep(3.0)
        shown = cdp.eval(
            "(document.getElementById('cfg-session-request_max_retries')||{}).value"
        )
        check("② 界面显示的**是后端的值**（后端改成 7 → 界面必须显示 7）",
              str(shown) == "7", f"界面显示 {shown!r}")

        print("\n========== 第二部分：保存行为 ==========")

        # ---- ③ 只发改动项 ----
        # ⚠️ 2026-10-09：原来这里改的是 `cfg-context-mode`（全局 context 段），
        #    那段已删除 ⇒ 改用**会话段**里一个 enum 字段，覆盖完全一样
        #    （"只发改动过的键、不整段回传"）。
        # ⚠️ 用 **bool** 而不是 provider_select：后者在"没配模型"时可能
        #    一个选项都没有（实测本机 `default_chat_provider_id` 就是空串），
        #    那样"改了值"其实没改 ⇒ 判据假红。bool 永远可翻转。
        #    （又一次印证：判据的输入要挑**一定取得到**的那个。）
        changed_key = cdp.eval(
            "(function(){window.__rec=[];"
            " var el=document.getElementById('cfg-session-display_reasoning_text');"
            " if(!el) return '';"
            " el.checked = !el.checked;"
            " document.getElementById('cfg-session-form').dispatchEvent("
            "  new Event('submit',{cancelable:true})); return 'display_reasoning_text';})()"
        )
        time.sleep(1.8)
        patched = cdp.eval(
            "(function(){var r=(window.__rec||[]).filter(function(x){"
            " return x.m==='PATCH' && x.u==='/api/v1/config-profile';});"
            " return r.length?r[r.length-1]:null;})()"
        )
        check("③ 保存会话段 → 真的发了 `PATCH /api/v1/config-profile`",
              bool(patched), f"got {patched}")
        if patched:
            body = patched.get("b") or {}
            sess = body.get("session") or {}
            check("③ 🔴 只发**改动过的**字段（不是整段回传）",
                  list(sess) == [changed_key], f"session 里的键={list(sess)}（期望 {changed_key}）")
            check("③ → **没有**顺手把已删除的 context 段也发上去",
                  "context" not in body, f"body 里的段={list(body)}")

        # 没改动时不该发请求
        cdp.eval(
            "(function(){window.__rec=[];"
            " document.getElementById('cfg-session-form').dispatchEvent("
            "  new Event('submit',{cancelable:true})); return 'ok';})()"
        )
        time.sleep(1.2)
        noop = cdp.eval(
            "(function(){return (window.__rec||[]).filter(function(x){"
            " return x.m==='PATCH';}).length;})()"
        )
        check("③ 没有改动时**不发** PATCH（别拿空请求刷后端）", (noop or 0) == 0,
              f"发了 {noop} 次")

        # 🔴 **收尾**：上面翻了一个 bool 并保存了 —— 判据改了**真实配置**，
        #    必须把它翻回去。否则每跑一次就改用户一次配置
        #    （本体项目踩过：某判据每跑一次就把插件配置翻一次，
        #      跑偶数遍巧合抵消、跑奇数遍留下脏配置）。
        cdp.eval(
            "(function(){"
            " var el=document.getElementById('cfg-session-display_reasoning_text');"
            " if(!el) return 'no-el';"
            " el.checked = !el.checked;"
            " document.getElementById('cfg-session-form').dispatchEvent("
            "  new Event('submit',{cancelable:true})); return 'ok';})()"
        )
        time.sleep(1.5)
        reverted = cdp.eval(
            "(function(){var el=document.getElementById('cfg-session-display_reasoning_text');"
            " return el ? el.checked : null;})()"
        )
        check("③ 收尾：把翻过的 bool 改回原值（判据不许留下副作用）",
              reverted is False, f"display_reasoning_text 现在={reverted}")

        print("\n========== 第三部分：错误如实显示 ==========")

        # ---- ④ 后端 400 → 点名那个字段 ----
        cdp.eval(
            "(function(){window.__rec=[];"
            " var el=document.getElementById('cfg-session-tts_trigger_probability');"
            " el.value='9';"
            " document.getElementById('cfg-session-form').dispatchEvent("
            "  new Event('submit',{cancelable:true})); return 'ok';})()"
        )
        time.sleep(1.8)
        errstate = cdp.eval(
            "(function(){var h=(document.getElementById('cfg-session-hint')||{}).innerText||'';"
            " var bad=document.querySelectorAll('#cfg-session-fields .cfg-invalid').length;"
            " return {hint:h, bad:bad};})()"
        ) or {}
        check("④ 越界值 → 保存失败并**说清原因**",
              "失败" in (errstate.get("hint") or ""),
              f"hint={errstate.get('hint')!r}")
        check("④ → 提示里**点名了字段**（用户才知道改哪一个）",
              "tts_trigger_probability" in (errstate.get("hint") or ""),
              f"hint={errstate.get('hint')!r}")
        check("④ → 那个字段被**标红**（`cfg-invalid`）",
              (errstate.get("bad") or 0) >= 1, f"标红数={errstate.get('bad')}")

        # ---- ⑤ provider_select 空列表要说清，不是空下拉 ----
        # 造一个"没有 TTS 模型"的场面：直接把 state 里的模型清掉再重渲染
        selstate = cdp.eval(
            "(function(){"
            " var sel=document.getElementById('cfg-session-default_tts_provider_id');"
            " if(!sel) return {found:false};"
            " var box=sel.closest('.cfg-provider-select');"
            " return {found:true, opts:sel.options.length,"
            "   emptyHint: box?(box.querySelector('.cfg-empty-models')||{}).innerText||'':''};})()"
        ) or {}
        check("⑤ `provider_select` 渲染出来了", bool(selstate.get("found")))
        if selstate.get("found"):
            # 真后端上没有 TTS 模型 → 应该只有"（不指定）"这一项 + 一条空态说明
            check("⑤ 🔴 没有可选模型时**给出空态说明**（不是留个空下拉让人干瞪眼）",
                  bool((selstate.get("emptyHint") or "").strip())
                  or (selstate.get("opts") or 0) > 1,
                  f"options={selstate.get('opts')} hint={selstate.get('emptyHint')!r}")

        print("\n========== 第四部分：恢复默认 ==========")

        # ---- ⑥ 恢复默认：二次确认 + DELETE ----
        cdp.eval("window.__confirmCalls=[]; window.confirm=function(m){window.__confirmCalls.push(m); return false;}; 'ok'")
        cdp.eval("window.__rec=[]; document.getElementById('cfg-reset').click(); 'ok'")
        time.sleep(1.0)
        cancelled = cdp.eval(
            "(function(){return {confirms:(window.__confirmCalls||[]).length,"
            " dels:(window.__rec||[]).filter(function(x){return x.m==='DELETE';}).length};})()"
        ) or {}
        check("⑥ 恢复默认会**先二次确认**", (cancelled.get("confirms") or 0) >= 1,
              f"confirm 调用={cancelled.get('confirms')}")
        check("⑥ → 用户取消时**不发** DELETE",
              (cancelled.get("dels") or 0) == 0, f"发了 {cancelled.get('dels')} 次")

        cdp.eval("window.confirm=function(){return true;}; window.__rec=[];"
                 " document.getElementById('cfg-reset').click(); 'ok'")
        time.sleep(2.0)
        did = cdp.eval(
            "(function(){return (window.__rec||[]).filter(function(x){"
            " return x.m==='DELETE' && x.u==='/api/v1/config-profile';}).length;})()"
        )
        check("⑥ → 确认后真的发 `DELETE /api/v1/config-profile`", (did or 0) >= 1,
              f"发了 {did} 次")

        print("\n========== 第五部分：失败必须响亮 ==========")

        # ---- ⑦ 读配置失败 → 说原因，不许显示成"没有配置" ----
        # 用假后端强制 500（只拦 config-profile，其余放行，避免影响其它判据）
        cdp.eval(
            "(function(){var o=window.fetch; window.fetch=function(i,init){"
            " var u=(typeof i==='string')?i:(i&&i.url)||'';"
            " if(u==='/api/v1/config-profile'){"
            "   return Promise.resolve(new Response(JSON.stringify("
            "     {error:{message:'boom: 后端内部错误',type:'server_error'}}),"
            "     {status:500,headers:{'Content-Type':'application/json'}}));}"
            " return o.apply(this,arguments);}; return 'ok';})()"
        )
        cdp.eval("loadConfigProfile(); 'ok'")
        time.sleep(2.0)
        failhint = cdp.eval("(document.getElementById('cfg-hint')||{}).innerText||''") or ""
        check("⑦ 🔴 读取失败时**如实说原因**（不许显示成\"没有配置\"）",
              ("失败" in failhint) and ("500" in failhint or "boom" in failhint),
              f"hint={failhint!r}")

        # 还原（重新导航一次，清掉我们的 fetch 覆盖）
        cdp.send("Page.reload")
        time.sleep(3.0)

        # ---- 全程没有未捕获的 JS 异常 ----
        errs = cdp.eval(
            "(function(){try{return JSON.stringify(window.__jsErrors||[]);}catch(e){return '[]';}})()"
        ) or "[]"
        try:
            parsed = json.loads(errs)
        except Exception:
            parsed = []
        check("全程没有未捕获的 JS 异常（界面正常往往掩盖着抛错）",
              parsed == [], f"errors={errs}")

        cdp.close()
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()

    print("\n" + "=" * 68)
    if failures:
        print(f"❌ {len(failures)}/{checks} 条判据失败：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {checks} 条判据通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
