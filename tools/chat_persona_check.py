#!/usr/bin/env python3
"""聊天页「当前人设」提示判据（真浏览器，Chrome CDP）。

🔴 守的 bug（情绪 2026-09-21 报障「聊天页面显示不带人格」）：

    会话绑的人设           = 糯米茨     ← 聊天**真的**会用它
    全局 active_persona_id = （空）
    → 面板聊天页显示 "未设当前人设（聊天将不带人设）"   ← **界面在说谎**

真实优先级在 `ChatService.resolve_persona`（`domain/chat.py:266`）：
**显式 id > 会话绑定的人设 > 全局当前人设**，而 `app.py:432` 调它时
**带了 `session_id`** → 会话绑定那层是**生效的**。
所以提示必须照**同一个优先级**算 —— 只看全局就是谎报。

为什么必须真浏览器：`curl` 能验 `/api/v1/sessions` 返回了 `persona_id`，
**验不了那行提示有没有照它渲染** —— 而 bug 恰恰只在 JS 里。

🔴 **本工具只用「真 UI + 真 HTTP」**，不碰 `window.state`：
   `state` 是 `app.js` 里的**模块级 `let`**（`app.js:6`），**不在 `window` 上**
   —— 我第一版判据写 `window.state.…` 拿到 `undefined`，
   于是 ⓪ 前置直接失败（**我的工具写错了，不是产品有问题**）。
   改成"点真导航 + 读真 DOM + 发真请求"之后，验的就是**用户真会走的路径**。

用法:
    python3 tools/chat_persona_check.py [BASE_URL] [TOKEN]

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
        # 错误录音机必须在**页面脚本之前**装（否则 boot() 早跑完了 = 假绿）
        #
        # ⚠️ 顺手也记一个 **fetch 计数器**（`window.__fetchSeq`）：
        #    本工具的若干判据要在"页面加载完"之后操作 DOM，而
        #    `selectPersona` 是**异步**的 —— 抢跑的话刚加的 DOM 会被
        #    随后的重渲染冲掉（实测踩到：`before=0 after=0`，看着像产品坏了）。
        #    页面上原本没有可靠的"网络空了"信号，所以在最外层包一次 fetch
        #    自己记序号：**序号不变 = 没有在飞的请求**。
        cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            source=(
                "window.__jsErrors=[];"
                "window.addEventListener('error',function(e)"
                "{window.__jsErrors.push(String(e.message));});"
                "window.__fetchSeq=0;window.__fetchLive=0;window.__urls=[];"
                "(function(){var of=window.fetch;window.fetch=function(u){"
                "  window.__fetchSeq++;window.__fetchLive++;"
                "  try{window.__urls.push(String(u));"
                "      if(window.__urls.length>400)window.__urls.shift();}catch(e){}"
                "  return of.apply(this,arguments).finally(function(){"
                "    window.__fetchLive--;});};})();"
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
        cdp.send("Page.reload")          # 真刷新，确保走 boot()
        time.sleep(4.0)

        print("\n========== 聊天页人设提示 ==========")

        # ---- 前置：面板数据到位（用**真 DOM**判断，不看 window.state）----
        #   `#session-select` 的 option 数 = 真会话数；
        #   `#s-persona` 的 option 数 = 人设数 + 1（首项是"（当前人设）"）。
        #   ⚠️ 判据只看 `#session-select`（会话下拉）与 `#persona-list`（人设列表）：
        #      `#s-persona`（会话详情里那个"绑定人设"下拉）**只有选中会话后才填**
        #      —— 我第一版要求它 `options.length>1`，于是**永远等不到**
        #      （实测：它恒为 1 = 只有"（当前人设）"占位项）。
        #      **别拿"还没渲染的控件"当前置条件**（实测踩到，前置直接假红）。
        ready = False
        for _ in range(40):
            ready = bool(cdp.eval(
                "(function(){var ss=document.getElementById('session-select');"
                " var pl=document.getElementById('persona-list');"
                " return !!(ss && ss.options.length && pl"
                "           && pl.innerText.trim() && pl.innerText.indexOf('加载失败')<0);})()"
            ))
            if ready:
                break
            time.sleep(0.4)
        if not check("⓪ 前置：会话与人设都已加载（真 DOM）", ready,
                     "会话下拉或人设列表没回来"):
            print(f"\n❌ {len(failures)} 条失败")
            return 1

        # ---- 前置：直接问后端要"哪个会话绑了人设"（真 HTTP，不猜 JS 内部状态）----
        bound = cdp.eval(
            "(async function(){var r=await fetch('/api/v1/sessions');"
            " var d=await r.json();"
            " var b=(d.data||[]).filter(function(s){return s.persona_id;});"
            " return {n:b.length, id:b.length?b[0].id:'',"
            "         pid:b.length?b[0].persona_id:''};})()",
            await_promise=True,
        ) or {}
        if not check(
            "① 前置：存在一个『绑定了人设』的会话（本判据的前提）",
            (bound.get("n") or 0) > 0,
            "没有绑定人设的会话 —— 请在面板上给某个会话绑一个人设再跑",
        ):
            print(f"\n❌ {len(failures)} 条失败")
            return 1

        # ---- 前置：全局「当前人设」为空（这才是能复现该 bug 的场景）----
        act = cdp.eval(
            "(async function(){var r=await fetch('/api/v1/personas');"
            " var d=await r.json(); return d.active_persona_id||'';})()",
            await_promise=True,
        )
        check(
            "② 前置：全局『当前人设』为空（复现场景；非空则本判据测不出该 bug）",
            not act,
            f"active_persona_id={act!r} —— 请先取消『当前人设』再跑",
        )

        # ---- 用**真 UI**选中那个绑了人设的会话：改 `#session-select` 并派发 change ----
        switched = cdp.eval(
            "(function(){var sel=document.getElementById('session-select');"
            " var want=" + json.dumps(str(bound.get("id") or "")) + ";"
            " var hit=[].slice.call(sel.options).filter(function(o){return o.value===want;})[0];"
            " if(!hit) return 'no-option';"
            " sel.value=want;"
            " sel.dispatchEvent(new Event('change')); return 'ok';})()"
        )
        time.sleep(2.5)      # 等 loadHistory 往返完成
        check("③ 能在「会话」下拉里选中那个会话（真 UI 操作）",
              switched == "ok", f"got {switched!r}")

        hint = str(cdp.eval("(document.getElementById('chat-hint')||{}).innerText") or "")
        # 后端告诉我们那个人设**叫什么名字**（真 HTTP）
        pname = cdp.eval(
            "(async function(){var r=await fetch('/api/v1/personas/'+encodeURIComponent("
            + json.dumps(str(bound.get("pid") or "")) + "));"
            " if(!r.ok) return ''; var d=await r.json(); return d.name||'';})()",
            await_promise=True,
        ) or ""

        # ---- ④ 核心：提示里要出现**会话绑的**那个人设名 ----
        check(
            "④ 聊天页提示显示**会话绑定**的人设",
            bool(pname) and pname in hint,
            f"期望含 {pname!r}，实际 {hint!r}",
        )
        # ---- ⑤ 不能再说"未设当前人设"（这就是那句谎话）----
        check(
            "⑤ 不再谎报『未设当前人设（聊天将不带人设）』",
            "未设当前人设" not in hint,
            f"实际 {hint!r}",
        )
        # ---- ⑥ 要标明来源（否则用户不知道这人设是哪来的）----
        check("⑥ 标明来源是『本会话绑定』", "本会话绑定" in hint, f"实际 {hint!r}")

        # ---- ⑦ **真 UI**：把该会话的人设解绑（走真 PATCH），提示要回落 ----
        #     ⚠️ 做完**必须还原**（这是用户真实数据，判据不许留痕）。
        orig_pid = str(bound.get("pid") or "")
        unbound = cdp.eval(
            # ⚠️ 用 `encodeURIComponent(...)` 而不是直接把 `json.dumps` 的产物
            #    塞进单引号 JS 字符串：`json.dumps("abc")` 是 `"abc"`（**带双引号**），
            #    拼进去 URL 就成了 `/sessions/"abc"` → **404**（实测踩到：
            #    ⑦⑧ 两条判据因为这个假红，而产品其实是对的）。
            "(async function(){var r=await fetch('/api/v1/sessions/'"
            "+encodeURIComponent(" + json.dumps(str(bound.get("id") or "")) + "),"
            " {method:'PATCH',headers:{'Content-Type':'application/json'},"
            "  body:JSON.stringify({persona_id:''})});"
            " if(!r.ok) return 'http-'+r.status;"
            " return 'ok';})()",
            await_promise=True,
        )
        # 走真导航回聊天页（applyRoute → NAV_LOADERS.chat → 会重画提示）
        cdp.eval("location.hash='#/session'; 'ok'")
        time.sleep(1.5)
        cdp.eval("location.hash='#/chat'; 'ok'")
        time.sleep(2.5)
        fell = str(cdp.eval("(document.getElementById('chat-hint')||{}).innerText") or "")
        check(
            "⑦ 会话不绑人设时 → 提示不再说『本会话绑定』",
            "本会话绑定" not in fell,
            f"解绑结果={unbound!r}，实际提示 {fell!r}",
        )
        # 还原（无论上面成败都要还原）
        restored = cdp.eval(
            "(async function(){var r=await fetch('/api/v1/sessions/'"
            "+encodeURIComponent(" + json.dumps(str(bound.get("id") or "")) + "),"
            " {method:'PATCH',headers:{'Content-Type':'application/json'},"
            "  body:JSON.stringify({persona_id:" + json.dumps(orig_pid) + "})});"
            " return r.ok;})()",
            await_promise=True,
        )
        check("⑧ 判据做完**把会话人设还原**（不留痕）", bool(restored), "还原失败")

        # ---- ⑨ 还原后再走一次真导航，提示要回到"本会话绑定" ----
        cdp.eval("location.hash='#/session'; 'ok'")
        time.sleep(1.2)
        cdp.eval("location.hash='#/chat'; 'ok'")
        time.sleep(2.5)
        again = str(cdp.eval("(document.getElementById('chat-hint')||{}).innerText") or "")
        check(
            "⑨ 还原后提示回到『本会话绑定 + 人设名』",
            "本会话绑定" in again and (not pname or pname in again),
            f"实际 {again!r}",
        )

        # ---- ⑩ 无未捕获 JS 错误 ----
        errs = cdp.eval("JSON.stringify(window.__jsErrors||[])") or "[]"
        check("⑩ 无未捕获 JS 错误", errs in ("[]", ""), f"{errs}")

        # ---- ⑪⑫ 「额外片段」表不再重复显示 4 个核心字段 ----
        #
        # 情绪 2026-09-21：「图二的人设栏**这个提示词片段可以不要了**」。
        # 那 4 行（角色设定/性格/场景/创作者备注）是从上面 4 个输入框
        # **现算**出来的（后端 `merge_persona_sections` 每次按字段重算），
        # 在表里再显示一遍 = 同一内容同页出现两次，而且**改那行不会生效**
        # → 纯误导。现在只显示"真正的额外片段"。
        #
        # 为什么必须真浏览器：这是**渲染结果** —— `curl` 拿到的 JSON 里
        # `prompt_sections` 仍然含那 4 段（它们确实存在于数据里，只是不该
        # 在表里重复展示），接口层看不出区别，**只有 DOM 能证明**。
        cdp.eval(
            "(function(){location.hash='#/persona/'+"
            + json.dumps(str(bound.get("pid") or ""))
            + "; return 1;})()"
        )
        # ⚠️ **必须等"人设详情真的加载完"再往下走**（实测踩到两次）：
        #    设 hash 只触发路由，`selectPersona` 是**异步**的
        #    （内部要先 `loadPersonas`）。若在它完成前动 DOM，
        #    刚加的那行会被随后的 `renderSections()` **整个重渲染冲掉**
        #    → 实测 `before=0 after=0`，看着像产品坏了，其实是判据抢跑。
        #
        #    ✅ 判据要两层，缺一不可：
        #      ① `#sections-block` 不再 hidden 且 `p-name` 有值
        #         （= 已经渲染过一轮）；
        #      ② **在飞的 fetch 归零且序号不再增长**
        #         （= 不会再有一次迟到的重渲染把我们的 DOM 冲掉）。
        #    只做①不够 —— 实测①已经满足，但随后那次 fetch 回来又重渲染了。
        def _settle(limit: float = 25.0, idle: float = 1.0):
            """等页面进入"没有在飞请求"的静止态。

            Returns:
                `(是否静止, 诊断串)` —— 诊断串带上计数器最终值，
                **失败时能看出是"还有请求在飞"还是"计数器根本没装上"**。
            """
            deadline = time.time() + limit
            last_seq, last_change = -1, time.time()
            seq, live, ticks = 0, 0, 0
            while time.time() < deadline:
                raw = cdp.eval(
                    "(function(){return JSON.stringify({s:window.__fetchSeq||0,"
                    " l:window.__fetchLive||0,"
                    " has:(typeof window.__fetchSeq!=='undefined')});})()"
                )
                try:
                    st = json.loads(raw or "{}")
                except Exception:
                    st = {}
                seq, live = int(st.get("s") or 0), int(st.get("l") or 0)
                if not st.get("has"):
                    return False, "fetch 计数器没装上（__fetchSeq 未定义）"
                if seq != last_seq:
                    last_seq, last_change, ticks = seq, time.time(), ticks + 1
                elif live == 0 and (time.time() - last_change) >= idle:
                    return True, f"静止于 seq={seq}（变化 {ticks} 次）"
                time.sleep(0.25)
            return False, f"超时仍在动：seq={seq} live={live}（变化 {ticks} 次）"

        def _top_urls(n: int = 5) -> str:
            """最常被请求的 URL（诊断"谁在刷"用）。"""
            raw = cdp.eval(
                "(function(){var u=window.__urls||[];var m={};"
                " u.forEach(function(x){m[x]=(m[x]||0)+1;});"
                " var a=Object.keys(m).map(function(k){return [k,m[k]];});"
                " a.sort(function(p,q){return q[1]-p[1];});"
                " return JSON.stringify(a.slice(0,%d));})()" % n
            )
            return str(raw or "[]")

        loaded = False
        for _ in range(40):      # 最多等 20s
            loaded = bool(
                cdp.eval(
                    "(function(){var b=document.getElementById('sections-block');"
                    " var n=document.getElementById('p-name');"
                    " if(!b||!n) return false;"
                    " return !b.classList.contains('hidden') && !!n.value;})()"
                )
            )
            if loaded:
                break
            time.sleep(0.5)
        settled, why = _settle()
        check(
            "⑪-前置 人设详情**加载并静止**（异步没跑完就操作 = 判据抢跑假红）",
            loaded and settled,
            f"loaded={loaded} settled={settled} · {why} · 刷得最凶的 URL: {_top_urls()}",
        )
        # ---- ⑪-路由 **进人设详情后不许无限自刷**（真 bug，本工具撞见的）----
        #
        # 🔴 2026-09-21 实测抓到的**生产 bug**（不是判据问题）：
        #    `applyRoute` 的 persona 分支调 `selectPersona(id)` **没传
        #    `skipHash`** → `selectPersona` 里 `go("persona/<id>")` 又写 hash
        #    → `hashchange` → `applyRoute` → **无限循环**。
        #
        #    实测数字：进详情后**每 3 秒约 2000 次** `/api/v1/personas`
        #    （4 秒内涨到 4130 次）；浏览器卡死、页面点不动，
        #    而且**零报错**（每次都 HTTP 200）——
        #    单测、`curl`、接口判据**全都看不见**它。
        #
        #    我是靠"页面永远不静止"（`__fetchSeq` 一直涨）才撞见的。
        #    ✅ 现在把它变成**显式判据**：等 3 秒，请求数不许再涨。
        seq_a = int(
            cdp.eval("window.__fetchSeq||0") or 0
        )
        time.sleep(3.0)
        seq_b = int(
            cdp.eval("window.__fetchSeq||0") or 0
        )
        check(
            "⑪-路由 进人设详情后**不自刷**（曾经每 3 秒 ~2000 次请求）",
            (seq_b - seq_a) <= 5,
            f"3 秒内新增 {seq_b - seq_a} 次请求（seq {seq_a}→{seq_b}）"
            f" —— 路由自环；刷得最凶的: {_top_urls()}",
        )
        time.sleep(0.3)
        row_titles = cdp.eval(
            "(function(){var rs=document.querySelectorAll('#sections-body tr');"
            " return JSON.stringify([].slice.call(rs).map(function(r){"
            "   var i=r.querySelector('input[type=text]'); return i?i.value:''; }));})()"
        ) or "[]"
        try:
            titles = json.loads(row_titles)
        except Exception:
            titles = []
        core = {"角色设定", "性格", "场景", "创作者备注"}
        dupes = [t for t in titles if t in core]
        check(
            "⑪ 人设页「额外片段」表不再重复显示 4 个核心字段",
            not dupes,
            f"表里出现了核心字段 {dupes}（它们由上方输入框决定，改这儿不生效）",
        )

        # ---- ⑫ **真的加一个额外片段 → 它必须出现在表里** ----
        #
        # 🔴 为什么必须做这一步（我的判据盲区，注入验证抓到的）：
        #    上面 ⑪ 只断言"表里**没有**核心字段"。而糯米茨这个人设
        #    **本来就没有额外片段** → 表格渲染逻辑即使**整个坏掉**
        #    （一行都不渲染），⑪ 照样绿（**假通过**）。
        #    实测：把 `renderSections` 的 `rows.forEach` 改成空数组后，
        #    ⑪ 通过 —— 因为它只检查"有没有多"，不检查"该有的在不在"。
        #
        # ✅ 所以这里**真的点「+ 添加片段」**，断言新行出现。
        #    ⚠️ 只改**前端内存**、**绝不保存**（不碰用户数据）：
        #       不点「保存片段」就不会发 PUT。
        # ⚠️ **`.empty-row` 也是一个 `tr`** —— 第一版判据数的是
        #    `#sections-body tr`，于是"空态行"被当成了一条数据行：
        #    before=1（空态行）→ after=1（真行替换了空态行）→
        #    断言 `after == before + 1` **假红**（产品其实是对的）。
        #    ✅ 只数**真数据行**（带文本框的那些）。
        before_state = cdp.eval(
            "(function(){var rs=document.querySelectorAll("
            "'#sections-body tr:not(.empty-row)'); return rs.length;})()"
        ) or 0
        cdp.eval("document.getElementById('add-section').click()")
        time.sleep(0.8)
        after = cdp.eval(
            "(function(){var rs=document.querySelectorAll("
            "'#sections-body tr:not(.empty-row)'); return rs.length;})()"
        ) or 0
        new_titles = cdp.eval(
            "(function(){var rs=document.querySelectorAll("
            "'#sections-body tr:not(.empty-row)');"
            " return JSON.stringify([].slice.call(rs).map(function(r){"
            "   var i=r.querySelector('input[type=text]'); return i?i.value:''; }));})()"
        ) or "[]"
        try:
            nt = json.loads(new_titles)
        except Exception:
            nt = []
        check(
            "⑫ 点「+ 添加片段」→ 新片段**真的出现在表里**",
            after == before_state + 1 and "新片段" in nt,
            f"before={before_state} after={after} titles={nt}",
        )
        # ⑬ 它绝不能是核心字段名（新增的必须走"额外片段"那条路）
        check(
            "⑬ 新增的片段不是核心字段（核心 4 段不由表格管理）",
            not [t for t in nt if t in core],
            f"titles={nt}",
        )

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
