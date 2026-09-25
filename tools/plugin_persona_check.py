#!/usr/bin/env python3
"""「陪伴面板 → 快速从已有人格导入」判据（真浏览器，Chrome CDP）。

🔴🔴 守的是情绪 2026-09-21 报的那个 bug（**原话**：「陪伴那边获取不到平台的默认人设」）：

    点「生成导入草稿」→ 界面永远弹
    「生成失败：还没有读取到可用的主回复人格文本，请先让 Bot 触发一次对话或检查人格配置」

根因**不是**"没触发过对话"（那句文案是**误诊**，见下），而是宿主的
compat `Context` **完全没有 `persona_manager` 属性**：

    page_api._roleplay_persona_prompt_for_id        (page_api.py:14799)
      └─ plugin._refresh_default_persona_prompt("") (daily_state.py:11073)
           └─ manager = getattr(context, "persona_manager", None)   → None
                └─ getter = get_default_persona_v3 if manager else None → None
                     └─ DEFAULT_PERSONA_PROMPT_FALLBACK            (daily_state.py:163)
      └─ prompt.startswith("未读取到 AstrBot 默认人格") → 报错      (page_api.py:14849)

⚠️ 那句文案会把排查带偏：它让用户"让 Bot 触发一次对话"，但
   `_refresh_default_persona_prompt` 的两个缓存写入口都在 `manager` 分支**之后**
   → `manager is None` 时**永远不写缓存**，触发了也修不好，且**一行日志都没有**。

为什么必须真浏览器：`curl` 能验 `/page/roleplay/draft_from_persona` 返回了什么，
**验不了"用户在面板上点那个按钮"这条完整链路**（桥接垫片 → 插件 JS → 上游 → 渲染）。

用法:
    python3 tools/plugin_persona_check.py [BASE_URL] [TOKEN]

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

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6190/").rstrip("/")

#: 陪伴面板的插件页路径（与我们 `plugin_pages_web` 的挂载形状一致）。
#: ⚠️ **必须以 `/` 结尾**：插件页用相对路径取资源，少斜杠会打到上一级 → 白屏。
PANEL = "/api/v1/plugins/astrbot_plugin_private_companion/pages/companion-panel/"

#: 「生成导入草稿」按钮的 id（实测 `companion-panel/index.html:467`）
DRAFT_BTN = "generateRoleplayDraftBtn"
#: 草稿预览容器（实测 `index.html:471`）
DRAFT_PANEL = "roleplayPersonaDraftPanel"
#: 「当前来源」那个只读标签（实测 `index.html:442`）
SOURCE_LABEL = "currentPersonaDisplay"

#: 那句**误诊**文案的特征串（判据要明确确认它**不再出现**）
MISDIAGNOSIS = "还没有读取到可用的主回复人格文本"


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
         "--remote-allow-origins=*", "--window-size=1600,1400", "about:blank"],
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
        # 错误录音机 + toast 录音机都必须在**页面脚本之前**装。
        #
        # 🔴 toast 录音为什么要**改写 `showToast` 本身**（而不是用 MutationObserver）：
        #    插件的 `showToast()`（`app.js:33828`）**复用同一个 `#pageToast` 节点**，
        #    每次只是 `toast.textContent = text` —— **没有新增节点**。
        #    我第一版用 MutationObserver 监听"新增节点里的失败字样" →
        #    **恒为空数组** → 于是"失败文案没出现"这条判据**假绿**
        #    （实测：把 persona_manager 摘掉复现 bug 后，⑦ 仍然通过）。
        #    → 必须在函数入口处记录**每一次**调用（含 text 与 tone）。
        cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            source=(
                "window.__jsErrors=[]; window.__toasts=[];"
                "window.addEventListener('error',function(e)"
                "{window.__jsErrors.push(String(e.message));});"
                # 钩住 showToast：等它被定义后包一层（DOMContentLoaded 时脚本已执行完）
                "(function(){function wrap(){"
                "  if(typeof window.showToast!=='function'||window.__toastWrapped) return;"
                "  var orig=window.showToast; window.__toastWrapped=true;"
                "  window.showToast=function(msg,tone){"
                "    try{window.__toasts.push({text:String(msg==null?'':msg).slice(0,400),"
                "                             tone:String(tone||'ok')});}catch(e){}"
                "    return orig.apply(this,arguments);};"
                "}"
                " if(document.readyState==='loading')"
                "   document.addEventListener('DOMContentLoaded',wrap);"
                " else wrap();"
                # 有些构建里 showToast 不是 window 属性而是模块内函数 →
                # 那就退回 MutationObserver **并同时观察 characterData**
                "  var mo=new MutationObserver(function(recs){"
                "    recs.forEach(function(r){"
                "      var n=r.target&&r.target.nodeType===3?r.target.parentElement:r.target;"
                "      if(!n||n.nodeType!==1) return;"
                "      var t=(n.innerText||n.textContent||'').trim();"
                "      if(t&&/失败|错误|未找到|Error/.test(t)) window.__toasts.push({text:t.slice(0,400),tone:'dom'});"
                "    });});"
                " function observe(){if(document.body)"
                "   mo.observe(document.body,{childList:true,subtree:true,characterData:true});}"
                " if(document.readyState==='loading')"
                "   document.addEventListener('DOMContentLoaded',observe);"
                " else observe();"
                "})();"
            ),
        )

        cdp.send("Page.navigate", url=BASE + "/")
        time.sleep(3.0)
        cdp.eval(
            "(function(){var t=document.getElementById('login-token');"
            " t.value=" + json.dumps(token) + ";"
            " document.getElementById('login-form').dispatchEvent("
            " new Event('submit',{cancelable:true})); return 'ok';})()"
        )
        time.sleep(2.5)
        cdp.send("Page.navigate", url=BASE + PANEL)
        time.sleep(8.0)

        print("\n========== 陪伴面板 · 快速从已有人格导入 ==========")

        # ---- ① 插件页真的起来了（不是白屏 / 不是"未检测到桥接"）----
        shell = cdp.eval(
            "(function(){return {title:document.title,"
            " tabs:document.querySelectorAll('[data-tab]').length,"
            " bridge:typeof window.AstrBotPluginPage};})()"
        ) or {}
        if not check(
            "① 陪伴面板已加载（有页签 + 同源桥垫片在位）",
            (shell.get("tabs") or 0) > 5 and shell.get("bridge") == "object",
            f"got {shell}",
        ):
            print(f"\n❌ {len(failures)} 条失败")
            return 1

        # ---- ② 切到「世界知识」页签（导入面板在这一页）----
        #   ⚠️ 不切页签的话 `loadRoleplayPersonas()` 根本不会被调用
        #      （实测：它挂在 `ensureTabData` 的 roleplay/config 分支上）
        #      → `#currentPersonaDisplay` 会一直停在"读取中..."。
        #      **这不是 bug，是"那一页还没打开"** —— 判据必须先把页签点开。
        cdp.eval(
            "(function(){var b=document.querySelector('[data-tab=\"roleplay\"]');"
            " if(b) b.click(); return 'ok';})()"
        )
        time.sleep(5.0)
        tabbed = cdp.eval(
            "(function(){var s=document.querySelector('.persona-import-panel');"
            " return {panel:!!s, open:!!(s&&s.open)};})()"
        ) or {}
        check("② 已切到「世界知识」页签且导入面板存在", bool(tabbed.get("panel")), f"{tabbed}")

        # ---- ③ 展开该面板（<details>），并等「当前来源」算出来 ----
        cdp.eval(
            "(function(){var d=document.querySelector('.persona-import-panel');"
            " if(d) d.open=true; return 'ok';})()"
        )
        src = ""
        for _ in range(30):
            src = str(cdp.eval(
                f"(document.getElementById('{SOURCE_LABEL}')||{{}}).innerText"
            ) or "")
            if src and "读取中" not in src:
                break
            time.sleep(1.0)
        check(
            "③ 「当前来源」算出来了（不是一直『读取中...』）",
            bool(src) and "读取中" not in src,
            f"got {src!r}",
        )

        # ---- ④ 插件真的拿到了人格列表（≥3 项：继承 + 我们的人设 + 内置 default）----
        # 🔴 用**真 HTTP** 数，不读 `state.roleplayPersonas` ——
        #    那是插件页里的局部变量，**不在 `window` 上**
        #    （内核面板那边已经因为同一个假设假红过一次）。
        # 🔴 用**插件自己的** `fetchJson()`（它内部有 `bridgeEndpointCandidates`
        #    的候选重试循环：`page/x` → `x` → `/x` → `插件名/page/x`）。
        #
        #    ⚠️ 我第一版直接调裸 `AstrBotPluginPage.apiGet('page/roleplay/personas')`
        #       → 拿到 `{"error":"未找到该路由","detail":"404"}` → **判据假红**。
        #       原因是绕过了插件那一层重试：单个候选名恰好不是我们挂载的那个。
        #       **通则：验"插件拿到了什么"就要走插件自己的取数函数**，
        #       自己拼一个调用等于把被测对象的一部分替换掉了。
        personas = cdp.eval(
            "(async function(){"
            "  try{"
            "    if(typeof fetchJson!=='function') return ['NO-fetchJson'];"
            "    var r=await fetchJson('/roleplay/personas');"
            "    var items=(r&&r.items)||[];"
            "    return items.map(function(p){return p.label||p.id;});"
            "  }catch(e){ return ['ERR:'+e.message]; }"
            "})()",
            await_promise=True,
        ) or []
        ours = [x for x in personas if "糯米茨" in str(x)]
        check(
            "④ 插件那一层能拿到人格列表，且**含我们的人设**（糯米茨）",
            bool(ours) and len(personas) >= 3,
            f"got {len(personas)} 项：{personas}",
        )

        # ---- ⑤ 核心：点「生成导入草稿」**不再弹那句误诊文案** ----
        cdp.eval("window.__toasts=[]; 'ok'")
        clicked = cdp.eval(
            f"(function(){{var b=document.getElementById('{DRAFT_BTN}');"
            " if(!b) return 'no-btn'; b.click(); return 'clicked';})()"
        )
        check("⑤ 找得到「生成导入草稿」按钮并点得动", clicked == "clicked", f"got {clicked!r}")

        # 等草稿出现（要打上游，可能十几秒）
        appeared = False
        for _ in range(70):
            appeared = bool(cdp.eval(
                f"(function(){{var p=document.getElementById('{DRAFT_PANEL}');"
                " return !!(p && !p.hidden && (p.innerText||'').trim());})()"
            ))
            if appeared:
                break
            time.sleep(1.0)

        toasts_raw = cdp.eval("JSON.stringify(window.__toasts||[])") or "[]"
        try:
            _t = json.loads(toasts_raw)
            toasts = " | ".join(
                (x.get("text") if isinstance(x, dict) else str(x)) for x in _t
            )
        except Exception:
            toasts = str(toasts_raw)
        # ---- ⑥ 草稿真的生成了 ----
        check("⑥ 点「生成导入草稿」→ **草稿真的生成出来了**", appeared,
              f"toasts={toasts}")
        # ---- ⑦ 那句误诊文案**不再出现** ----
        check(
            "⑦ 不再出现误诊文案『还没有读取到可用的主回复人格文本』",
            MISDIAGNOSIS not in toasts,
            f"toasts={toasts}",
        )
        # ---- ⑧ 草稿正文来自**真实人设**（不是空壳/占位）----
        body = str(cdp.eval(
            f"(document.getElementById('{DRAFT_PANEL}')||{{}}).innerText"
        ) or "")
        check("⑧ 草稿正文有实质内容（>200 字）", len(body) > 200, f"len={len(body)}")

        # ---- ⑨ 无未捕获 JS 错误 ----
        errs = cdp.eval("JSON.stringify(window.__jsErrors||[])") or "[]"
        check("⑨ 无未捕获 JS 错误", errs in ("[]", ""), f"{errs}")

        print()
        if failures:
            print(f"❌ {len(failures)}/{checks} 条失败：")
            for f in failures:
                print(f"   - {f}")
            return 1
        print(f"✅ 全部 {checks} 条通过")
        return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()


if __name__ == "__main__":
    raise SystemExit(main())
