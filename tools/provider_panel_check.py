#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「模型供应商」面板判据（真浏览器，Chrome CDP）—— **两层结构**版。

🔴 为什么必须用真浏览器：
    `curl` 只能证明**接口**返回了什么，而本页的价值几乎全在 JS 侧：
    左侧源列表有没有渲染出来、点一下右侧是不是真的加载了那个源的详情、
    key 输入框有没有被回显、没选中源时是不是白屏、「获取模型列表」点了有没有反应。
    本项目已多次踩过"接口全绿、界面空白"（人设导入那次），
    所以端到端判据必须落到 **DOM**。

🔴 判据不许"跳过"（项目铁律）：
    前置条件不满足就是**失败**，不许打一句"跳过"就算 ✅ ——
    "跳过判据 = 假通过，比不写还糟"（mobile_check.py 第 4 项的真实教训）。
    → 为了让"数据路径"这一类判据**任何时候都能真跑**（不等后端），
      本脚本内置一个**照冻结契约写的假后端**（§ 见 FAKE_BACKEND_JS），
      在页面里替换 `window.fetch`，把列表渲染 / 选中加载 / key 只写 /
      获取模型列表 / 添加模型 / 测试成功与失败 / 错误如实显示 **真的走一遍**。

⚠️ 诚实边界（必须说清，别把假后端当"已联通"）：
    * **第一阶段（真后端）** 证明的是「面板 ↔ 真后端 有没有接上」；
      后端没就绪时它**不跳过**，而是要求面板**如实报错**（那也是一条判据）。
    * **第二阶段（假后端）** 证明的是「**前端照契约实现是否正确**」，
      **不**证明真后端与前端已联通。真后端就绪后重跑第一阶段即可获得该结论。

用法：
    .venv/bin/python tools/provider_panel_check.py [base_url] [token]
    （token 留空则自动从 .env 的 EMOTION_AUTH_ACCESS_TOKEN 读，**会 strip 掉单引号**）
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_check import CHROME, PORT, CDP, _http_json  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6191/"

#: 侧边栏文案（用户要求的原文）
NAV_LABEL = "模型供应商"
#: 列表路由
ROUTE = "#/providers"

#: ============================================================================
#: 假后端：**照冻结契约**（docs/ASTRBOT_PROVIDER_CONTRACT.md §2/§3）实现，
#: 响应形状 = AstrBot 的 `{status, message, data}` 信封。
#:
#: 为什么连信封都照抄：契约 §1 明确"这里对齐 AstrBot 的信封"，
#: 而本项目其它接口是**裸对象**。前端必须两种都认 —— 只认一种的话另一条路
#: 会读成 undefined → 界面显示"还没有提供商源"，**这就是最贵的静默失败**。
#: 本阶段顺带把这条路验了（下面"裸对象兼容"那条判据）。
#: ============================================================================
FAKE_BACKEND_JS = r"""
(function () {
  // confirm() 在 headless 里没有 dialog 处理器会阻塞事件循环 → 直接放行
  window.confirm = function () { return true; };

  var DB = {
    provider_sources: [
      // ★ 对话类（本页要显示的）
      {id:"syuan", provider:"openai", type:"openai_chat_completion",
       provider_type:"chat_completion", key:["sk-secret-alpha-0123456789"],
       api_base:"https://api.syuan.org/v1", timeout:60, proxy:"",
       custom_headers:{"X-Trace":"abc"}, enable:true},
      {id:"mxgpt", provider:"openai", type:"openai_chat_completion",
       provider_type:"chat_completion", key:[],
       api_base:"https://api.mxzzz.xyz/v1", timeout:120, proxy:"",
       custom_headers:{}, enable:true},
      // ★ 非对话类：**不该显示**，但要被如实计数（不静默丢数据）
      {id:"embed-src", provider:"openai", type:"openai_embedding",
       provider_type:"embedding", key:["sk-embed"], api_base:"https://x/v1",
       timeout:20, proxy:"", custom_headers:{}, enable:true}
    ],
    providers: [
      {id:"syuan/gemini-3.5-flash", provider_source_id:"syuan",
       model:"gemini-3.5-flash", enable:true}
    ],
    available: {
      "syuan": ["gemini-3.5-flash", "deepseek-v4", "gpt-5.6-sol", "qwen3-max"]
    }
  };
  // 录音机：记下每个请求的 method/url/body（判据要断言 PUT 到底发了什么）
  window.__calls = [];
  window.__fail = {};        // 例：{list: 500} → 强制某条路由失败

  // ---- 厂商预设（契约 §3）----
  // ⚠️ 至少要覆盖三组分类 + `executable_by_us` 的 true/false，
  //    否则"按分类过滤""不可执行要标注"这两条判据在桩上永远验不到。
  var SUPPORTED_PTYPES = ["chat_completion", "text_to_speech", "embedding"];
  var PRESETS = [
    {id:"openai", label:"OpenAI", provider:"openai",
     type:"openai_chat_completion", provider_type:"chat_completion",
     api_base:"https://api.openai.com/v1", note:"OpenAI 兼容格式（/chat/completions）",
     executable_by_us:true},
    {id:"openai-responses", label:"OpenAI (Responses)", provider:"openai",
     type:"openai_responses", provider_type:"chat_completion",
     api_base:"https://api.openai.com/v1", note:"Responses 格式（/responses）",
     executable_by_us:true},
    {id:"anthropic", label:"Anthropic", provider:"anthropic",
     type:"anthropic_chat_completion", provider_type:"chat_completion",
     api_base:"https://api.anthropic.com/v1", note:"需 AstrBot 适配器，本平台暂不能执行",
     executable_by_us:false},
    {id:"edge_tts", label:"Edge TTS（免 Key）", provider:"microsoft",
     type:"edge_tts", provider_type:"text_to_speech",
     api_base:"", note:"需 AstrBot 适配器，本平台暂不能执行",
     executable_by_us:false},
    {id:"openai_embedding", label:"OpenAI Embedding", provider:"openai",
     type:"openai_embedding", provider_type:"embedding",
     api_base:"", note:"需 AstrBot 适配器，本平台暂不能执行",
     executable_by_us:false}
  ];

  function json(obj, status) {
    return Promise.resolve(new Response(JSON.stringify(obj), {
      status: status || 200, headers: {"Content-Type": "application/json"}
    }));
  }
  function ok(data, message) { return json({status:"ok", message:message||null, data:data}); }
  function err(msg, status) {
    return json({status:"error", message:msg, data:null}, status || 400);
  }
  function srcPublic(s) {
    return {id:s.id, provider:s.provider, type:s.type, provider_type:s.provider_type,
            api_base:s.api_base, timeout:s.timeout, proxy:s.proxy,
            custom_headers:s.custom_headers, enable:s.enable,
            // 🔴 契约：列表接口只给"有没有 key"，**不给 key 本身**
            key_configured: !!(s.key && s.key.length)};
  }
  function findSrc(id) {
    for (var i=0;i<DB.provider_sources.length;i++)
      if (DB.provider_sources[i].id === id) return DB.provider_sources[i];
    return null;
  }

  var orig = window.fetch;
  window.fetch = function (input, init) {
    var url = (typeof input === "string") ? input : ((input && input.url) || "");
    var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    var body = null;
    try { body = init && init.body ? JSON.parse(init.body) : null; } catch (e) {}

    if (url.indexOf("/api/v1/") !== 0) return orig.apply(this, arguments);
    window.__calls.push({method:method, url:url, body:body});

    var q = url.split("?")[1] || "";
    // 🔴 query 参数名照**真实 AstrBot 契约**（实测 `Query(...)`）：
    //    源用 `source_id`、模型用 `provider_id`。
    //    ⚠️ 我原来只认 `id=` → 前端改用正确参数名后，stub 反而取不到 id
    //       → 桩返回 404 → 判据假红。**桩必须长得像真的**（本项目通则）。
    var qid = decodeURIComponent(
      (q.match(/(?:^|&)(?:source_id|provider_id|id)=([^&]*)/) || [])[1] || ""
    );

    // ---- 错误注入开关（判据要用它验"错误如实显示"）----
    // ⚠️ 这里必须**整体相等**：真实 URL 是 `/api/v1/provider-sources`。
    //    我原来写 `url.indexOf("/provider-sources") === 0`（永不成立）
    //    → 错误注入是**死的** → 两条"错误如实显示"判据必然假红。
    //    通则：桩里的路径匹配要精确相等，别用"看着像路径开头"的裸片段。
    if (window.__fail.list && method === "GET" && url === "/api/v1/provider-sources") {
      return err("boom: 后端内部错误", window.__fail.list);
    }
    if (window.__fail.models && /\/models$/.test(url)) {
      return err("上游 401：key 无效", window.__fail.models);
    }
    // 预设目录也留一个注入开关（本轮新增）：验"失败要响亮，不许静默变空列表"。
    if (window.__fail.presets && url.indexOf("/api/v1/provider-presets") === 0) {
      return err("boom: 预设目录读不到", window.__fail.presets);
    }

    // ---- 厂商预设目录（契约 §3）----
    // 🔴 必须补这一条：前端「+ 新增」现在会先开预设弹窗，
    //    弹窗要拉 `/api/v1/provider-presets`。桩不答 → 404 →
    //    弹窗停在错误态 → 后面所有"新增/保存"的判据全部连锁假红（实测 22 条）。
    //    教训：**给前端加了新取数点，桩就要同步补上**，否则判据测的是桩的缺口。
    if (method === "GET" && url.indexOf("/api/v1/provider-presets") === 0) {
      var ptq = (url.split("?")[1] || "").match(/(?:^|&)provider_type=([^&]*)/);
      var wantPt = ptq ? decodeURIComponent(ptq[1]) : "";
      var pool = PRESETS.filter(function (p) { return !wantPt || p.provider_type === wantPt; });
      if (wantPt && !SUPPORTED_PTYPES.some(function (t) { return t === wantPt; })) {
        return err("未知的 provider_type：" + wantPt, 400);
      }
      return ok({presets: pool, total: pool.length});
    }

    // ---- 端点层 ----
    if (method === "GET" && url === "/api/v1/provider-sources") {
      return ok({provider_sources: DB.provider_sources.map(srcPublic)});
    }
    if (method === "POST" && url === "/api/v1/provider-sources") {
      var ns = {id:body.id, provider:body.provider||"openai", type:body.type||"openai_chat_completion",
                provider_type:body.provider_type||"chat_completion",
                key:body.key||[], api_base:body.api_base||"", timeout:body.timeout||120,
                proxy:body.proxy||"", custom_headers:body.custom_headers||{}, enable:true};
      DB.provider_sources.push(ns);
      return ok(null, "更新 provider source 成功");
    }
    if (method === "PUT" && url.indexOf("/api/v1/provider-sources/by-id") === 0) {
      var t = findSrc(qid);
      if (!t) return err("provider source 不存在：" + qid, 404);
      t.id = body.id || qid;
      t.api_base = body.api_base; t.timeout = body.timeout;
      t.proxy = body.proxy; t.custom_headers = body.custom_headers;
      // 🔴 空 key = **不修改**（契约语义：改了模型名不该把 key 抹掉）
      if (body.key && body.key.length) t.key = body.key;
      return ok(null, "更新 provider source 成功");
    }
    if (method === "DELETE" && url.indexOf("/api/v1/provider-sources/by-id") === 0) {
      for (var i=0;i<DB.provider_sources.length;i++)
        if (DB.provider_sources[i].id === qid) DB.provider_sources.splice(i,1);
      return ok(null, "删除 provider source 成功");
    }

    // ---- 源下已配置的模型 ----
    var mProv = url.match(/^\/api\/v1\/provider-sources\/([^\/]+)\/providers$/);
    if (mProv) {
      var sid = decodeURIComponent(mProv[1]);
      if (method === "GET") {
        return ok({providers: DB.providers.filter(function(p){return p.provider_source_id===sid;}),
                   model_metadata:{}});
      }
      if (method === "POST") {   // 加模型
        DB.providers.push({id:body.id || (sid+"/"+body.model), provider_source_id:sid,
                           model:body.model, enable:true});
        return ok(null, "新增成功");
      }
    }
    // ---- 可用模型列表（「获取模型列表」）----
    var mMod = url.match(/^\/api\/v1\/provider-sources\/([^\/]+)\/models$/);
    if (mMod && method === "GET") {
      var s2 = decodeURIComponent(mMod[1]);
      var list = DB.available[s2] || [];
      var md = {};
      list.forEach(function (m) {
        md[m] = {id:m, reasoning:m.indexOf("gpt")>=0, tool_call:true,
                 modalities:{input:["text","image"], output:["text"]},
                 limit:{context:128000}};
      });
      return ok({models:list, provider_source_id:s2, model_metadata:md});
    }

    // ---- 模型层：测试 / 删除 ----
    var mTest = url.match(/^\/api\/v1\/providers\/([^\/]+)\/test$/);
    if (mTest && method === "POST") {
      var pid = decodeURIComponent(mTest[1]);
      // 🔴 失败也用**真实契约形状**（`ASTRBOT_PROVIDER_CONTRACT.md` §3）：
      //    `{status:"unavailable", error:"<原因>"}` —— 与真实 AstrBot 一致。
      //    ⚠️ 我原来编了个 `{ok:false, reason:...}`（**契约里没有的字段**）→
      //       前端按契约读 `error` 就取不到 → 判据假红。
      //       **桩必须长得像真的**（本项目通则）。
      if (window.__fail.test) return ok({id:pid, model:pid.split("/").pop(),
                                        type:"chat_completion", name:pid,
                                        status:"unavailable",
                                        error:"upstream_auth：上游 401，key 无效"});
      // ★ 成功也用 AstrBot 的形状（status:"available"）
      return ok({id:pid, model:pid.split("/").pop(), type:"chat_completion",
                 name:pid, status:"available", error:null});
    }
    var mDel = url.match(/^\/api\/v1\/providers\/([^\/]+)$/);
    if (mDel && method === "DELETE") {
      var did = decodeURIComponent(mDel[1]);
      DB.providers = DB.providers.filter(function(p){return p.id!==did;});
      return ok(null, "删除成功");
    }
    // 非对话源的模型列表等我们不请求；真收到就如实 404（不假装成功）
    return err("not found: " + method + " " + url, 404);
  };
})();
"""


def read_token() -> str:
    """从 .env 读访问令牌。

    ⚠️ **值带单引号**（`run.sh` 用 `source` 读会去掉，我们用 grep|cut 不会）——
       上一轮就是漏了 `tr -d "'\\""`，拿带引号的令牌去登录失败，
       被误判成"产品 bug"（服务端 token 指纹 dc996a71 与去引号后一致）。
       **验证失败时先证明"输入是对的"，再怀疑被测对象。**
    """
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("EMOTION_AUTH_ACCESS_TOKEN"):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def select_source(cdp, source_id: str) -> bool:
    """点左侧列表里的某个源，并**返回它是否真的点到了**。

    🔴 为什么要有这个 helper（不是洁癖）：
        原来各处都写成 `var li=…; if(li) li.click(); return 'ok';` ——
        **元素不在时静默返回 'ok'**，点击变空气，判据随即在"没选中源"的状态下
        去读 `ps-available-hint`，报出「先选中（或保存）一个提供商源。」这种
        **跟真实根因完全无关**的失败信息（实测踩到）。
        **通则：`if(el) el.click()` 这种写法等于把"没点到"伪装成"点到了"；
             凡是要用它当下一步前置条件的，必须把"在不在"当返回值传出来。**
    """
    found = cdp.eval(
        "(function(){var li=document.querySelector('#provider-source-list "
        f'li[data-source-id=\\"{source_id}\\"]\');'
        " if(!li) return false; li.click(); return true;})()"
    )
    return bool(found)


def main() -> int:
    failures: list[str] = []
    checks = 0
    phase_fail: dict[str, int] = {}

    def check(label: str, ok: bool, detail: str = "", phase: str = "?") -> bool:
        nonlocal checks
        checks += 1
        if ok:
            print(f"  ✅ {label}")
        else:
            failures.append(label)
            phase_fail[phase] = phase_fail.get(phase, 0) + 1
            print(f"  ❌ {label} — {detail}")
        return bool(ok)

    token = sys.argv[2] if len(sys.argv) > 2 else read_token()
    print(f"  · base={BASE} token指纹={hashlib.sha256(token.encode()).hexdigest()[:8]}")

    proc = subprocess.Popen(
        [
            CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
            "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            # ⚠️ 新版 Chrome 默认拒绝跨源 WebSocket 握手（Handshake status 403），必须显式放行
            "--remote-allow-origins=*",
            "--window-size=1400,950", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = ""
        for _ in range(40):
            time.sleep(0.25)
            try:
                tabs = _http_json("/json")
                page = next((t for t in tabs if t.get("type") == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                continue
        if not ws_url:
            print("❌ Chrome 未就绪")
            return 1

        cdp = CDP(ws_url)
        try:
            cdp.send("Runtime.enable")
            cdp.send("Page.enable")
            cdp.send("Page.navigate", url=BASE)
            time.sleep(2.5)

            # 控制台报错要能看见（JS 抛异常时页面往往"看着正常"）
            cdp.eval(
                "window.__jsErrors=[];"
                "window.addEventListener('error',function(e){window.__jsErrors.push(String(e.message));});"
                "'ok'"
            )

            status = cdp.eval(
                "fetch('/api/v1/auth/login',{method:'POST',"
                "headers:{'Content-Type':'application/json'},"
                f"body:JSON.stringify({{token:{json.dumps(token)}}})}})"
                ".then(r=>r.status)",
                await_promise=True,
            )
            if not check("登录成功（否则后面全是 401 假象）", status == 200, f"status={status}", "登录"):
                print("\n❌ 登录失败，后续判据无法可信执行。")
                return 1

            probe = cdp.eval("fetch('/api/v1/provider-sources').then(r=>r.status)", await_promise=True)
            backend_ok = probe == 200
            print(f"  · GET /api/v1/provider-sources → HTTP {probe}"
                  + ("（后端已就绪）" if backend_ok else "（后端未就绪 → 面板须如实报错）"))

            # =====================================================================
            print("\n========== 第一阶段：侧边栏 / 路由 / 空态（与后端是否就绪无关）==========")
            # =====================================================================
            cdp.send("Page.navigate", url=BASE + ROUTE)
            time.sleep(3.0)

            # ---- 1. 侧边栏入口 ----
            nav = cdp.eval(
                "(function(){"
                "  var a = document.querySelector('.nav-item[data-nav=\"providers\"]');"
                "  if (!a) return {found:false};"
                "  var ico = a.querySelector('.nav-ico');"
                "  var lbl = a.querySelector('.nav-label');"
                "  var items=[].slice.call(document.querySelectorAll('.nav-item'));"
                "  var i=items.indexOf(a);"
                "  return {found:true, href:a.getAttribute('href'),"
                "          ico: ico ? ico.textContent.trim() : null,"
                "          label: lbl ? lbl.textContent.trim() : null,"
                "          nextIsSettingsOrLogs: i>=0 && i+1<items.length &&"
                "            ['settings','logs'].indexOf(items[i+1].dataset.nav)>=0};"
                "})()"
            ) or {}
            if check("① 侧边栏存在 data-nav=\"providers\" 的导航项",
                     nav.get("found"), f"got {nav}", "①侧边栏"):
                check("① → 文案是「模型供应商」", nav.get("label") == NAV_LABEL,
                      f"实际 {nav.get('label')!r}", "①侧边栏")
                check("① → href 指向 #/providers", nav.get("href") == ROUTE,
                      f"实际 {nav.get('href')!r}", "①侧边栏")
                check("① → 有 .nav-ico 图标（与相邻项同构）", bool(nav.get("ico")),
                      f"ico={nav.get('ico')!r}", "①侧边栏")
                # ⚠️ 2026-09-21：日志页插到了「设置」**之前** → 原来那条
                #    "供应商后面紧跟设置"的断言不再成立。**这是断言过时，不是产品缺陷**
                #    （改的是导航顺序，不是供应商页本身）。
                #    → 改成断言"供应商后面跟着的是**设置或日志**"，两条都算合法；
                #      仍然能抓住"供应商项被挪到别处/被删"这类真问题。
                check("① → 后面紧跟「设置」或「日志」", bool(nav.get("nextIsSettingsOrLogs")),
                      "它后面紧跟的不是 settings 项", "①侧边栏")

            # ---- 2. 收起态图标仍在（收起后只剩图标，这是唯一可辨识性）----
            cdp.eval("document.querySelector('.app').classList.add('collapsed')")
            time.sleep(0.6)
            collapsed = cdp.eval(
                "(function(){"
                "  var a=document.querySelector('.nav-item[data-nav=\"providers\"]');"
                "  if(!a) return {found:false};"
                "  var ico=a.querySelector('.nav-ico'), lbl=a.querySelector('.nav-label');"
                "  return {found:true, ico: ico?ico.textContent.trim():null,"
                "          icoVisible: ico?!!(ico.offsetWidth||ico.offsetHeight||ico.getClientRects().length):false,"
                "          labelHidden: lbl?(lbl.offsetWidth===0&&lbl.offsetHeight===0):false,"
                "          navWidth: Math.round(a.getBoundingClientRect().width)};"
                "})()"
            ) or {}
            check("② 收起态下该导航项**图标仍可见**（不是缺失/错位）",
                  bool(collapsed.get("icoVisible")) and bool(collapsed.get("ico")),
                  f"ico={collapsed.get('ico')!r} visible={collapsed.get('icoVisible')}", "②收起态")
            check("② 收起态确实生效（标签隐藏、条目收成图标条宽度）",
                  bool(collapsed.get("labelHidden")) and 0 < (collapsed.get("navWidth") or 999) <= 70,
                  f"labelHidden={collapsed.get('labelHidden')} navWidth={collapsed.get('navWidth')}",
                  "②收起态")
            cdp.eval("document.querySelector('.app').classList.remove('collapsed')")

            # ---- 3. hash 路由 + 刷新后仍在该页 ----
            check("③ 导航到了 #/providers", (cdp.eval("location.hash") or "") == ROUTE,
                  f"hash={cdp.eval('location.hash')!r}", "③路由")
            shown = cdp.eval(
                "(function(){var e=document.getElementById('tab-providers');"
                " return !!e && !e.classList.contains('hidden');})()"
            )
            check("③ → #tab-providers 已显示（不是白屏/仍停在上一个 panel）", bool(shown), "", "③路由")

            cdp.send("Page.reload")
            time.sleep(3.5)
            after = cdp.eval(
                "(function(){return {hash:location.hash,"
                "  panelShown:(function(){var e=document.getElementById('tab-providers');"
                "               return !!e && !e.classList.contains('hidden');})(),"
                "  navActive:(function(){var a=document.querySelector('.nav-item[data-nav=\"providers\"]');"
                "               return !!a && a.classList.contains('active');})()};})()"
            ) or {}
            check("③ 🔴 **刷新后仍停在 #/providers**（hash 路由的核心价值）",
                  after.get("hash") == ROUTE, f"刷新后 hash={after.get('hash')!r}", "③路由")
            check("③ → 刷新后 panel 仍显示", bool(after.get("panelShown")), f"{after}", "③路由")
            check("③ → 刷新后侧边栏该项仍高亮", bool(after.get("navActive")), f"{after}", "③路由")

            # ---- 8a. 未选中源 → 空态（不白屏）----
            empty_state = cdp.eval(
                "(function(){"
                "  var ph=document.getElementById('provider-source-placeholder');"
                "  var panel=document.getElementById('provider-source-panel');"
                "  var vis=function(e){return !!e && !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);};"
                "  return {phExists:!!ph, phVisible:vis(ph), phText: ph?ph.innerText.trim():'',"
                "          panelVisible:vis(panel)};"
                "})()"
            ) or {}
            check("⑧a 未选中任何源时显示**空态提示**（不是白屏）",
                  empty_state.get("phExists") and empty_state.get("phVisible")
                  and "请选择" in (empty_state.get("phText") or ""),
                  f"got {empty_state}", "⑧空态")
            check("⑧a → 空态时详情面板**不**显示（空态与详情互斥）",
                  not empty_state.get("panelVisible"), f"got {empty_state}", "⑧空态")

            # ---- 7a. 「获取模型列表」按钮存在且可点 ----
            # 🔴 **必须先选中一个源**：这个按钮在**详情面板里**，
            #    而上一段刚验完"未选中源 → 空态"（详情面板是隐藏的）→
            #    不选中就验可见性 = **判据自己造出来的假红**。
            #    ⚠️ 实测：未选中时 panelHidden=True、按钮 invisible；
            #       选中后两者都翻转。
            selected_for_btn = cdp.eval(
                "(function(){"
                "  var li=document.querySelector('#provider-source-list li');"
                "  if(!li) return false;"
                "  li.click(); return true;"
                "})()"
            )
            time.sleep(2.0)
            check("⑦ 已选中一个源（按钮可见性的前提）", bool(selected_for_btn),
                  "列表里没有可选的源 —— 无法验按钮可见性（前置条件不满足=失败）",
                  "⑦模型按钮")
            btn_state = cdp.eval(
                "(function(){"
                "  var b=document.getElementById('ps-fetch-models');"
                "  if(!b) return {found:false};"
                "  var dis=b.disabled;"
                "  var before=(document.getElementById('ps-available-hint')||{}).innerText||'';"
                "  return {found:true, disabled:dis, text:b.innerText.trim(),"
                "          visible:!!(b.offsetWidth||b.offsetHeight||b.getClientRects().length),"
                "          before:before};"
                "})()"
            ) or {}
            if check("⑦ 「获取模型列表」按钮存在", btn_state.get("found"), f"{btn_state}", "⑦模型按钮"):
                check("⑦ → 按钮可见且未禁用（可点）",
                      bool(btn_state.get("visible")) and not btn_state.get("disabled"),
                      f"{btn_state}", "⑦模型按钮")
                check("⑦ → 文案是「获取模型列表」", btn_state.get("text") == "获取模型列表",
                      f"实际 {btn_state.get('text')!r}", "⑦模型按钮")

            # ---- 3b/8b. 真后端这一段：有数据 must 渲染；没数据 must 报错 ----
            print("\n---------- 真后端数据路径（依赖后端是否就绪）----------")
            real = None
            for _ in range(30):
                real = cdp.eval(
                    "(function(){"
                    "  var hint=document.getElementById('providers-hint');"
                    "  var ul=document.getElementById('provider-source-list');"
                    "  var emp=document.getElementById('provider-source-empty');"
                    "  return {hint: hint?hint.innerText:'' ,"
                    "          items: ul?ul.querySelectorAll('li').length:-1,"
                    "          emptyVisible: !!emp && !emp.classList.contains('hidden'),"
                    "          emptyText: emp?emp.innerText.trim():''};"
                    "})()"
                ) or {}
                if real.get("hint"):
                    break
                time.sleep(0.4)
            print(f"  · 真后端：{real}")

            if backend_ok:
                if (real.get("items") or 0) > 0:
                    check("③ 真后端有数据 → 左侧源列表渲染出条目",
                          True, "", "③真数据")
                    first = cdp.eval(
                        "(function(){var li=document.querySelector('#provider-source-list li');"
                        " if(!li) return null;"
                        " return {id:(li.querySelector('.src-id')||{}).innerText||'',"
                        "         base:(li.querySelector('.src-base')||{}).innerText||'',"
                        "         hasDel: !!li.querySelector('button[data-del]')};})()"
                    ) or {}
                    check("③ → 列表项是**两行**（id + api_base）",
                          bool(first.get("id")) and bool(first.get("base")),
                          f"got {first}", "③真数据")
                    check("③ → 列表项右侧有删除按钮", bool(first.get("hasDel")), f"got {first}", "③真数据")
                    check("③ → 有数据时空态**不**显示", not real.get("emptyVisible"),
                          f"emptyVisible={real.get('emptyVisible')}", "③真数据")
                else:
                    check("③ 真后端就绪 → 必须显示空态文案（不是白屏）",
                          real.get("emptyVisible") and len(real.get("emptyText") or "") > 0,
                          f"{real}", "③真数据")
            else:
                # 🔴 后端未就绪：**不算通过也不算跳过** —— 面板必须如实报错
                h = real.get("hint") or ""
                check("⑧ 后端未就绪时，面板**如实显示错误**（不静默吞掉 —— 项目铁律）",
                      ("失败" in h) and ("404" in h or "HTTP" in h),
                      f"hint={h!r}", "⑧错误如实")
                check("⑧ → 失败时仍有可见空态/提示（不许白屏）",
                      bool(real.get("emptyVisible")) or (real.get("items") or 0) >= 0,
                      f"{real}", "⑧错误如实")

            # =====================================================================
            print("\n========== 第二阶段：假后端（照冻结契约）—— 数据路径真跑一遍 ==========")
            print("  ⚠️ 这一阶段证明「前端照契约实现正确」，**不**证明真后端已联通")
            # =====================================================================
            cdp.eval(FAKE_BACKEND_JS + "; 'fake-installed'")
            # 重跑路由让列表走假后端（location.hash 没变 → 显式调 applyRoute）
            cdp.eval("location.hash='#/providers'; applyRoute(); 'ok'")
            time.sleep(1.5)

            fake_list = None
            for _ in range(30):
                fake_list = cdp.eval(
                    "(function(){var ul=document.getElementById('provider-source-list');"
                    " return {items: ul?ul.querySelectorAll('li').length:-1,"
                    "         hint:(document.getElementById('providers-hint')||{}).innerText||''};})()"
                ) or {}
                if (fake_list.get("items") or 0) > 0:
                    break
                time.sleep(0.4)
            # ---- 3. 左侧源列表渲染 ----
            n_items = fake_list.get("items") or 0
            if check("③ 左侧「提供商源」列表渲染出条目（假后端 3 个源）",
                     n_items == 2, f"期望 2 个对话类源，实际 {n_items}（{fake_list.get('hint')!r}）",
                     "③列表渲染"):
                detail = cdp.eval(
                    "(function(){return [].slice.call(document.querySelectorAll('#provider-source-list li'))"
                    " .map(function(li){return {id:(li.querySelector('.src-id')||{}).innerText||'',"
                    "   base:(li.querySelector('.src-base')||{}).innerText||'',"
                    "   del:!!li.querySelector('button[data-del]')};});})()"
                ) or []
                check("③ → 每项都是**两行**（id + api_base）且带删除按钮",
                      all(d.get("id") and d.get("base") and d.get("del") for d in detail),
                      f"{detail}", "③列表渲染")
                check("③ → **只显示「对话」类**源（embedding 那个不该出现）",
                      all(d.get("id") != "embed-src" for d in detail), f"{detail}", "③列表渲染")
                check("③ → 被略过的非对话源**如实告知**（不静默丢数据）",
                      "未显示" in (fake_list.get("hint") or ""), f"hint={fake_list.get('hint')!r}",
                      "③列表渲染")

            # ---- 4. 点左侧源 → 右侧加载详情（id/api_base 预填）----
            clicked = cdp.eval(
                "(function(){var li=document.querySelector('#provider-source-list li[data-source-id=\"syuan\"]');"
                " if(!li) return 'notfound'; li.click(); return 'ok';})()"
            )
            time.sleep(1.2)
            detail = cdp.eval(
                "(function(){"
                "  var g=function(i){var e=document.getElementById(i); return e?e.value:null;};"
                "  var panel=document.getElementById('provider-source-panel');"
                "  var ph=document.getElementById('provider-source-placeholder');"
                "  var vis=function(e){return !!e && !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);};"
                "  return {title:(document.getElementById('provider-source-title')||{}).innerText||'',"
                "          subtitle:(document.getElementById('provider-source-subtitle')||{}).innerText||'',"
                "          id:g('ps-id'), base:g('ps-api-base'), timeout:g('ps-timeout'), proxy:g('ps-proxy'),"
                "          key:g('ps-api-key'), keyPh:(document.getElementById('ps-api-key')||{}).placeholder||'',"
                "          panelVisible:vis(panel), phVisible:vis(ph),"
                "          hash:location.hash,"
                "          active:(function(){var li=document.querySelector('#provider-source-list li.active');"
                "                   return li?li.dataset.sourceId:null;})()};"
                "})()"
            ) or {}
            if check("④ 点左侧源 → 右侧显示该源详情（面板可见、空态退场）",
                     detail.get("panelVisible") and not detail.get("phVisible"), f"got {detail}",
                     "④选中加载"):
                check("④ → 标题 = 源 id", detail.get("title") == "syuan", f"{detail.get('title')!r}",
                      "④选中加载")
                check("④ → 副标题 = api_base", detail.get("subtitle") == "https://api.syuan.org/v1",
                      f"{detail.get('subtitle')!r}", "④选中加载")
                check("④ → **ID 预填**", detail.get("id") == "syuan", f"{detail.get('id')!r}", "④选中加载")
                check("④ → **API Base URL 预填**", detail.get("base") == "https://api.syuan.org/v1",
                      f"{detail.get('base')!r}", "④选中加载")
                check("④ → 列表项高亮（选中态）", detail.get("active") == "syuan", f"{detail}", "④选中加载")
                check("④ → 路由跟着走到 #/providers/syuan（可后退/可刷新）",
                      detail.get("hash") == "#/providers/syuan", f"hash={detail.get('hash')!r}",
                      "④选中加载")

            # ---- 5. 🔴 key 框为空（不回显）+ 绝不泄漏到页面文本 ----
            check("⑤ 🔴 选中源后 **key 输入框为空**（绝不回显已存的 key）",
                  (detail.get("key") or "") == "", f"实际值长度={len(detail.get('key') or '')}",
                  "⑤key不回显")
            check("⑤ → 占位符说明了「已设置（留空 = 不修改）」",
                  "留空" in (detail.get("keyPh") or ""), f"placeholder={detail.get('keyPh')!r}",
                  "⑤key不回显")
            leak = cdp.eval(
                "(function(){var t=document.body?document.body.innerText:'';"
                " var m=t.match(/sk-[A-Za-z0-9_\\-]{8,}/g); return {hits:m?m.slice(0,3):[]};})()"
            ) or {}
            check("⑤ → 页面**可见文本**里没有疑似密钥（sk-…）", not leak.get("hits"),
                  f"发现 {leak.get('hits')}", "⑤key不回显")
            dom_leak = cdp.eval(
                "(function(){return document.body.innerHTML.indexOf('sk-secret-alpha') >= 0;})()"
            )
            check("⑤ → 连 **DOM 源码**里都没有那把 key（防写进 value/属性里）",
                  not dom_leak, "DOM 里出现了 sk-secret-alpha", "⑤key不回显")

            # ---- 高级配置（可折叠）三段齐全 ----
            blocks = cdp.eval(
                "(function(){"
                "  var d=document.getElementById('provider-advanced');"
                "  var s=document.querySelector('#provider-source-panel .providers-block');"
                "  var h4=[].slice.call(document.querySelectorAll('#provider-source-panel h4'))"
                "          .map(function(e){return e.innerText.trim();});"
                "  return {advTag: d?d.tagName:null, hasSummary: !!(d&&d.querySelector('summary')),"
                "          blocks:h4,"
                "          hasHeaderRows: !!document.getElementById('ps-header-rows'),"
                "          hasHeaderAdd: !!document.getElementById('ps-header-add'),"
                "          hdrRows: document.querySelectorAll('#ps-header-rows .header-row').length};"
                "})()"
            ) or {}
            check("④ 三段结构齐全（设置 / 高级配置… / 模型）",
                  all(any(x.startswith(k) for x in (blocks.get("blocks") or []))
                      for k in ("设置", "模型")) and bool(blocks.get("blocks")),
                  f"h4={blocks.get('blocks')}", "④选中加载")
            check("④ → 「高级配置…」是**可折叠**区块（<details><summary>）",
                  blocks.get("advTag") == "DETAILS" and blocks.get("hasSummary"), f"{blocks}",
                  "④选中加载")
            check("④ → 自定义请求头：已存的键值对渲染成行，且有「添加更多」",
                  (blocks.get("hdrRows") or 0) == 1 and blocks.get("hasHeaderAdd"),
                  f"hdrRows={blocks.get('hdrRows')}", "④选中加载")

            # ---- 7. 「获取模型列表」真的能拉到并列出候选 ----
            # 🔴 2026-09-21 产品变更（情绪：**「获取到的模型，与测试都用弹窗形式发送」**）：
            #    候选列表从右侧卡片**内联**改成了 `#models-modal` 弹层。
            #    所以这里不再只看"列表里有几个 li" —— 那样在弹层**根本没打开**时
            #    也会通过（列表节点存在于 DOM，`querySelectorAll` 照样数得到 4 个）。
            #    → 判据必须断言**弹层真的可见**（offsetWidth/offsetHeight/getClientRects），
            #      这正是"元素在 DOM 里 ≠ 用户看得见"的落点。
            def modal_vis_js(mid: str) -> str:
                return (
                    "(function(){var m=document.getElementById('" + mid + "');"
                    " if(!m) return {exists:false};"
                    " return {exists:true, hidden:m.classList.contains('hidden'),"
                    "         visible:!!(m.offsetWidth||m.offsetHeight||m.getClientRects().length)};})()"
                )

            before_open = cdp.eval(modal_vis_js("models-modal")) or {}
            check("⑦ 拉取**之前**「可用模型」弹层是关着的（否则下一条断言无意义）",
                  not before_open.get("visible"),
                  f"got {before_open}", "⑦模型按钮")
            cdp.eval("document.getElementById('ps-fetch-models').click()")
            avail = None
            for _ in range(30):
                avail = cdp.eval(
                    "(function(){var ul=document.getElementById('ps-available-list');"
                    " return {hidden: !ul || ul.classList.contains('hidden'),"
                    "         items: ul?ul.querySelectorAll('li').length:0,"
                    "         boxes: ul?ul.querySelectorAll('input[type=checkbox]').length:0,"
                    "         hint:(document.getElementById('ps-available-hint')||{}).innerText||''};})()"
                ) or {}
                if not avail.get("hidden") and (avail.get("items") or 0) > 0:
                    break
                time.sleep(0.4)
            opened = cdp.eval(modal_vis_js("models-modal")) or {}
            check("⑦ 🔴 点「获取模型列表」→「可用模型」**弹层真的打开了**（不是内联列表）",
                  bool(opened.get("visible")), f"got {opened}", "⑦模型按钮")
            check("⑦ 点「获取模型列表」→ **列出候选模型**（4 个，带勾选框）",
                  (avail.get("items") or 0) == 4 and (avail.get("boxes") or 0) >= 3,
                  f"got {avail}", "⑦模型按钮")
            check("⑦ → 弹层里带着**搜索框**（它跟着候选一起搬进弹层了）",
                  cdp.eval(
                      "(function(){var i=document.getElementById('ps-model-search');"
                      " if(!i) return false;"
                      " var m=document.getElementById('models-modal');"
                      " return !!m && m.contains(i) &&"
                      "        !!(i.offsetWidth||i.offsetHeight||i.getClientRects().length);})()"
                  ) is True,
                  "搜索框不在弹层里，或不可见", "⑦模型按钮")
            check("⑦ → 已配置的模型在候选里**打勾且禁用**（不会重复添加）",
                  cdp.eval(
                      "(function(){var li=document.querySelector('#ps-available-list li.disabled');"
                      " var cb=li?li.querySelector('input[type=checkbox]'):null;"
                      " return !!(cb && cb.checked && cb.disabled);})()"
                  ) is True,
                  "没有找到「已添加且禁用」的候选项", "⑦模型按钮")
            # 🔴 弹层列表**必须自己滚**：不限高的话几十条候选会把弹层拉得长过屏幕，
            #    底部的说明与关闭按钮一起被顶出视野（`.preset-list` 当初就是为这个加的限高）。
            #    判据是"限高**真的生效**"：CSS 规则存在但被更高优先级顶掉时 `maxHeight` 会是 none。
            scroll_box = cdp.eval(
                "(function(){var ul=document.getElementById('ps-available-list');"
                " if(!ul) return {found:false};"
                " var cs=getComputedStyle(ul);"
                " return {found:true, maxH:cs.maxHeight, overflowY:cs.overflowY};})()"
            ) or {}
            check("⑦ → 候选列表**限高 + 可滚动**（候选多了不会溢出屏幕）",
                  scroll_box.get("maxH") not in (None, "", "none")
                  and scroll_box.get("overflowY") in ("auto", "scroll"),
                  f"got {scroll_box}", "⑦模型按钮")

            # ---- 添加模型 → 已配置列表增长（🔴 并且**卡片里那份内联列表**要同步更新）----
            # 🔴 这条守的是产品要求 7：候选在弹层里，但"已配置的模型"**留在卡片上**
            #    （`#ps-configured-list` 是用户的工作列表，不是一次性的查询结果）。
            #    弹层里的勾选与卡片里的列表必须**同时**变 —— 否则用户关掉弹层后
            #    对着一张过期的列表发呆，以为没添加上去。
            card_before = cdp.eval(
                "(function(){var ul=document.getElementById('ps-configured-list');"
                " var m=document.getElementById('models-modal');"
                " return {n: ul?ul.querySelectorAll('li').length:-1,"
                "         inModal: !!(m && ul && m.contains(ul))};})()"
            ) or {}
            check("⑦ 🔴 已配置列表 `#ps-configured-list` **留在卡片里、没有被搬进弹层**",
                  card_before.get("inModal") is False and (card_before.get("n") or -1) >= 0,
                  f"got {card_before}", "⑦模型按钮")
            before_n = card_before.get("n") or 0
            cdp.eval(
                "(function(){var ul=document.getElementById('ps-available-list');"
                " var b=[].slice.call(ul.querySelectorAll('button'))"
                "        .filter(function(x){return x.innerText.trim()==='添加';})[0];"
                " if(b) b.click(); return b?'ok':'nobtn';})()"
            )
            time.sleep(1.2)
            after_n = cdp.eval("document.querySelectorAll('#ps-configured-list li').length") or 0
            check("④ 在弹层里点候选的「添加」→ 卡片里已配置模型列表 **+1**",
                  after_n == before_n + 1, f"{before_n} → {after_n}", "④选中加载")
            mid = cdp.eval(
                "(function(){var li=document.querySelector('#ps-configured-list li');"
                " var c=li?li.querySelector('code'):null;"
                " return {id:c?c.innerText:'',"
                "         count:(document.getElementById('ps-model-count')||{}).innerText||'',"
                "         hasTest: !!li && !!li.querySelector('button[data-op=\"test\"]'),"
                "         hasDel: !!li && !!li.querySelector('button[data-op=\"del\"]')};})()"
            ) or {}
            check("④ → 模型 id 是 `源id/模型id` 形状，且有「测试」「移除」",
                  "/" in (mid.get("id") or "") and mid.get("hasTest") and mid.get("hasDel"),
                  f"got {mid}", "④选中加载")
            check("④ → 「已配置 N」计数跟着更新",
                  (mid.get("count") or "").startswith("已配置"), f"{mid.get('count')!r}", "④选中加载")

            # ---- 关闭弹层：关闭按钮 / 遮罩 / Esc **三件套都要真的关掉它** ----
            # 🔴 为什么三种都验：这不是"多测一遍"——
            #    三种关闭走的是**三处独立代码**（`#models-close` 的 onclick、
            #    `#models-modal` 的遮罩 onclick、window 的 keydown 表），
            #    漏接任何一处都不会报错，只会"点了没反应"（静默失效）。
            #    遮罩那次点击**必须点在遮罩本身**（`e.target.id` 判定）——
            #    点弹层内部空白也会命中同一条 handler，所以这里点的是弹层**外面**的坐标。
            def modal_hidden(mid_: str) -> bool:
                return (cdp.eval(modal_vis_js(mid_)) or {}).get("visible") is False

            cdp.eval("document.getElementById('models-close').click()")
            time.sleep(0.3)
            check("⑦ 点弹层「关闭」按钮 → 弹层真的关掉了（回到 hidden）",
                  modal_hidden("models-modal"),
                  f"got {cdp.eval(modal_vis_js('models-modal'))}", "⑦模型按钮")
            cdp.eval("document.getElementById('ps-fetch-models').click()")
            time.sleep(1.5)
            check("⑦ 前置：弹层能被再次打开（下面要验遮罩关闭）",
                  bool((cdp.eval(modal_vis_js("models-modal")) or {}).get("visible")),
                  "重新点「获取模型列表」后弹层没打开", "⑦模型按钮")
            # 点在遮罩上（视口左上角，弹层是居中的 → 这里一定是遮罩本身）
            cdp.eval(
                "(function(){var m=document.getElementById('models-modal');"
                " m.dispatchEvent(new MouseEvent('click',{bubbles:true}));"
                " return 'ok';})()"
            )
            time.sleep(0.3)
            check("⑦ 点弹层**遮罩** → 弹层真的关掉了（遮罩点击不是只绑给预设弹层的）",
                  modal_hidden("models-modal"),
                  f"got {cdp.eval(modal_vis_js('models-modal'))}", "⑦模型按钮")
            # Esc：键盘可达性
            cdp.eval("document.getElementById('ps-fetch-models').click()")
            time.sleep(1.5)
            check("⑦ 前置：弹层已打开（下面要验 Esc 关闭）",
                  bool((cdp.eval(modal_vis_js("models-modal")) or {}).get("visible")),
                  "弹层没打开", "⑦模型按钮")
            cdp.eval(
                "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));"
                " window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));"
                "'ok'"
            )
            time.sleep(0.4)
            check("⑦ 按 **Esc** → 弹层真的关掉了（键盘可达性）",
                  modal_hidden("models-modal"),
                  f"got {cdp.eval(modal_vis_js('models-modal'))}", "⑦模型按钮")

            # ---- 每个模型可「测试」（成功 / 失败两种都要如实显示）----
            before_test = cdp.eval(modal_vis_js("test-modal")) or {}
            check("⑦ 测试**之前**「模型测试」弹层是关着的（否则下一条断言无意义）",
                  not before_test.get("visible"), f"got {before_test}", "⑦模型按钮")
            cdp.eval("document.querySelector('#ps-configured-list button[data-op=\"test\"]').click()")
            t_open = None
            for _ in range(20):
                t_open = cdp.eval(modal_vis_js("test-modal")) or {}
                if t_open.get("visible"):
                    break
                time.sleep(0.2)
            check("⑦ 🔴 点模型「测试」→「模型测试」**弹层真的打开了**（结果不在卡片里了）",
                  bool(t_open.get("visible")), f"got {t_open}", "⑦模型按钮")
            check("⑦ → 结果区 `providers-test-result` **就在弹层里**（不是页面上另一处）",
                  cdp.eval(
                      "(function(){var e=document.getElementById('providers-test-result');"
                      " var m=document.getElementById('test-modal');"
                      " return !!(e && m && m.contains(e));})()"
                  ) is True,
                  "结果节点不在 test-modal 里", "⑦模型按钮")
            time.sleep(1.2)
            t_ok = cdp.eval(
                "(function(){var e=document.getElementById('providers-test-result');"
                " return {hidden:!!e.classList.contains('hidden'), text:e.innerText};})()"
            ) or {}
            # ⚠️ 原来这里断言的是 `#providers-test-result` 自身**没有 hidden 类**
            #    （它当时是卡片里一个可隐藏的 `<pre>`）。现在它是弹层的**常驻**内容，
            #    显隐由弹层统一管 → 那条 `hidden` 断言已**过时**，换成"弹层可见 + 内容正确"，
            #    检测力不减（内容判据原样保留，并新增了弹层可见性这一条）。
            check("⑦ 点模型「测试」→ 弹层里显示**连通正常**（含 ✅）",
                  bool(t_open.get("visible")) and "✅" in (t_ok.get("text") or "")
                  and "连通正常" in (t_ok.get("text") or ""),
                  f"got {t_ok}", "⑦模型按钮")

            cdp.eval("window.__fail.test = true; 'ok'")
            cdp.eval("document.querySelector('#ps-configured-list button[data-op=\"test\"]').click()")
            time.sleep(1.2)
            t_bad = cdp.eval("document.getElementById('providers-test-result').innerText") or ""
            check("⑦ 测试失败时**逐条说清原因**（不是静默/不是假装成功）",
                  "❌" in t_bad and "upstream_auth" in t_bad, f"got {t_bad!r}", "⑦模型按钮")
            cdp.eval("window.__fail.test = false; 'ok'")
            # 测试弹层同样要能用关闭按钮关掉（失败信息读完了才关）
            cdp.eval("document.getElementById('test-close').click()")
            time.sleep(0.3)
            check("⑦ 点「模型测试」弹层的关闭按钮 → 真的关掉了（失败原因读得到、也关得掉）",
                  modal_hidden("test-modal"),
                  f"got {cdp.eval(modal_vis_js('test-modal'))}", "⑦模型按钮")

            # ⚠️ 「获取模型列表」**失败**时"弹层照样打开"那条判据放在下面（原本就在那里，
            #    紧挨着既有的 401 断言）—— 因为前面那几条要点 `source_id=syuan` 的保存判据
            #    依赖"当前选中的还是 syuan"，在这里切源会把它们变成假红。

            # ---- 保存：api_key **留空 = 不发 key 字段**（否则会把 key 写空/抹掉）----
            cdp.eval(
                "(function(){window.__calls=[];"
                " document.getElementById('ps-timeout').value='99';"
                " document.getElementById('provider-save-config').click();})()"
            )
            time.sleep(1.5)
            # 🔴 诊断：保存没发出时，把现场状况打出来（别只报 got None）
            if not cdp.eval("(window.__calls||[]).length"):
                diag = cdp.eval(
                    "(function(){"
                    "  var b=document.getElementById('provider-save-config');"
                    "  var panel=document.getElementById('provider-source-panel');"
                    "  return {btnExists:!!b, btnDisabled:b?b.disabled:null,"
                    "          panelHidden:panel?panel.classList.contains('hidden'):null,"
                    "          idVal:(document.getElementById('ps-id')||{}).value,"
                    "          baseVal:(document.getElementById('ps-api-base')||{}).value,"
                    "          hint:(document.getElementById('providers-hint')||{}).innerText};"
                    "})()"
                )
                print(f"     · 保存未发出，现场：{diag}")
                print(f"     · 本次全部请求：{cdp.eval(chr(39)+"(window.__calls||[]).map(function(c){return c.method+chr(32)+c.url})+chr(39)")}")
            # 🔴 取"最后一次 PUT"要**只返回可序列化的字段**：
            #    原来 `return c[c.length-1]` 返回整个对象（含 body 里可能不可序列化的东西），
            #    实测 `returnByValue` 静默给回 None → 判据假红（而请求其实**发出去了**）。
            #    实测证据：calls 里明明有 `PUT /api/v1/provider-sources/by-id?source_id=syuan`。
            put = cdp.eval(
                "(function(){var c=(window.__calls||[]).filter(function(x){"
                "   return x.method==='PUT' && x.url.indexOf('/api/v1/provider-sources/by-id')===0;});"
                " if(!c.length) return null;"
                " var last=c[c.length-1];"
                " return {url:String(last.url||''), method:String(last.method||''),"
                "         body:(last.body&&typeof last.body==='object')?JSON.parse(JSON.stringify(last.body)):null};"
                "})()"
            )
            check("⑤ 保存时请求打到 `PUT /provider-sources/by-id?source_id=<id>`",
                  # ⚠️ 参数名照**真实契约**是 `source_id`（不是 `id`）
                  bool(put) and "source_id=syuan" in (put or {}).get("url", ""),
                  f"got {put}", "⑤key不回显")
            if put:
                body = put.get("body") or {}
                check("⑤ 🔴 保存时 **key 框留空 → 请求体里没有 key 字段**（留空 = 不修改）",
                      "key" not in body, f"body={json.dumps(body, ensure_ascii=False)[:200]}",
                      "⑤key不回显")
                check("⑤ → 非敏感字段照常提交（timeout=99 已带上）", body.get("timeout") == 99,
                      f"timeout={body.get('timeout')!r}", "⑤key不回显")
                check("⑤ → 分类字段照契约带上 provider_type=chat_completion",
                      body.get("provider_type") == "chat_completion", f"{body.get('provider_type')!r}",
                      "⑤key不回显")
                check("⑤ → 请求体里**没有**明文 key", "sk-secret-alpha-0123456789" not in json.dumps(body),
                      "请求体里出现了 key", "⑤key不回显")

            # 填了 key → 必须带上（形状是**数组**，照契约）
            cdp.eval(
                "(function(){window.__calls=[];"
                " document.getElementById('ps-api-key').value='sk-newly-typed-key';"
                " document.getElementById('provider-save-config').click();})()"
            )
            time.sleep(1.5)
            put2 = cdp.eval(
                "(function(){var c=(window.__calls||[]).filter(function(x){"
                "   return x.method==='PUT' && x.url.indexOf('/api/v1/provider-sources/by-id')===0;});"
                " if(!c.length) return null;"
                " var last=c[c.length-1];"
                " return {url:String(last.url||''), method:String(last.method||''),"
                "         body:(last.body&&typeof last.body==='object')?JSON.parse(JSON.stringify(last.body)):null};"
                "})()"
            ) or {}
            b2 = put2.get("body") or {}
            check("⑤ 填了 key → 请求体带上 `key`，且形状是**数组**（照真实契约）",
                  isinstance(b2.get("key"), list) and b2["key"] == ["sk-newly-typed-key"],
                  f"key={b2.get('key')!r}", "⑤key不回显")
            reload_key = cdp.eval(
                "(function(){var li=document.querySelector('#provider-source-list li[data-source-id=\"syuan\"]');"
                " li.click(); return 'ok';})()"
            )
            time.sleep(1.2)
            key_after = cdp.eval("document.getElementById('ps-api-key').value")
            check("⑤ → 保存后再进该源，key 框**仍然是空的**（永不回显）",
                  (key_after or "") == "", f"值长度={len(key_after or '')}", "⑤key不回显")

            # ---- 新增（POST）----
            cdp.eval(
                "(function(){window.__calls=[];"
                " document.getElementById('provider-source-new').click();"
                " document.getElementById('ps-id').value='brand-new';"
                " document.getElementById('ps-api-base').value='https://api.example.com/v1';"
                " document.getElementById('ps-api-key').value='sk-brand';"
                " document.getElementById('provider-save-config').click();})()"
            )
            time.sleep(1.8)
            posted = cdp.eval(
                "(function(){var c=(window.__calls||[]).filter(function(x){"
                "   return x.method==='POST' && x.url==='/api/v1/provider-sources';});"
                " return c.length?c[c.length-1]:null;})()"
            ) or {}
            check("④ 「+ 新增」→ 保存走 `POST /provider-sources` 且带上新 id",
                  (posted.get("body") or {}).get("id") == "brand-new", f"got {posted}", "④选中加载")
            new_items = cdp.eval("document.querySelectorAll('#provider-source-list li').length") or 0
            check("④ → 新增后左侧列表变成 3 项（真的落盘了）", new_items == 3,
                  f"实际 {new_items}", "④选中加载")

            # ---- 删除（DELETE）----
            cdp.eval(
                "(function(){var b=document.querySelector('#provider-source-list li[data-source-id=\"brand-new\"] button[data-del]');"
                " if(b) b.click(); return b?'ok':'nobtn';})()"
            )
            time.sleep(1.5)
            after_del = cdp.eval("document.querySelectorAll('#provider-source-list li').length") or 0
            check("④ 点列表项右侧删除 → 该源从列表消失（DELETE 生效）", after_del == 2,
                  f"实际 {after_del}", "④选中加载")

            # ---- 8b. 错误必须如实显示（假后端返回 500）----
            cdp.eval("window.__fail.list = 500; applyRoute(); 'ok'")
            time.sleep(1.5)
            err_state = cdp.eval(
                "(function(){var h=(document.getElementById('providers-hint')||{}).innerText||'';"
                " var emp=document.getElementById('provider-source-empty');"
                " return {hint:h, emptyVisible: !!emp && !emp.classList.contains('hidden'),"
                "         emptyText: emp?emp.innerText.trim():''};})()"
            ) or {}
            check("⑧ 🔴 后端 500 时面板**如实显示错误**（不静默吞掉 —— 项目铁律）",
                  ("失败" in (err_state.get("hint") or "")) and ("500" in (err_state.get("hint") or "")),
                  f"hint={err_state.get('hint')!r}", "⑧错误如实")
            check("⑧ → 出错时仍然**有可见的空态/提示**（绝不白屏）",
                  bool(err_state.get("emptyVisible")) and bool(err_state.get("emptyText")),
                  f"{err_state}", "⑧错误如实")
            cdp.eval("window.__fail.list = 0; 'ok'")
            # 🔴 **必须把列表恢复回来**再往下走：500 注入让 `loadProviderSources()`
            #    把 `state.providerSources` 清成了 `[]`，光把开关关掉**不会重画**
            #    → 后面 ⑦ 那两组要点 `li[data-source-id="mxgpt"]` 的判据
            #      点了个**空气**（`if(li)` 静默跳过），失败信息变成
            #      「先选中（或保存）一个提供商源。」—— 与真实根因毫无关系。
            #    **通则：错误注入之后要显式恢复现场；`if(el)` 式的点击必须验它真点到了。**
            # ⚠️ 这里**不能**用 `location.hash = '#/providers'`：hash **本来就是**
            #    `#/providers`（上面 applyRoute 之后就到了这里）→ 赋值是**空操作**、
            #    不触发 hashchange → loader 永不重跑 → 列表一直是空的。
            #    实测就是这么假红一条。→ 直接调 `applyRoute()` 重跑一次。
            #    **通则：靠"改 hash/触发事件"来刷新，先确认新旧值真的不同。**
            cdp.eval("applyRoute(); 'ok'")
            time.sleep(1.5)
            restored = cdp.eval("document.querySelectorAll('#provider-source-list li').length") or 0
            check("⑧ → 关掉错误注入后列表**恢复**（后续判据的前置现场）", restored == 2,
                  f"列表项 {restored}（应为 2）", "⑧错误如实")

            # ---- 「获取模型列表」失败时也要如实说（这是最容易做成静默的一处）----
            cdp.eval("window.__fail.models=401; 'ok'")
            picked = select_source(cdp, "mxgpt")
            check("⑦ 前置：点得中源 `mxgpt`（点不中就说明列表没恢复）", picked,
                  f"#provider-source-list li[data-source-id=\"mxgpt\"] 不存在或点击失败", "⑦模型按钮")
            time.sleep(1.2)
            cdp.eval("document.getElementById('ps-fetch-models').click()")
            time.sleep(1.5)
            mfail = cdp.eval("(document.getElementById('ps-available-hint')||{}).innerText||''") or ""
            check("⑦ 「获取模型列表」失败时**逐条说清原因**（不静默、不假装空列表）",
                  "失败" in mfail and "401" in mfail, f"got {mfail!r}", "⑦模型按钮")
            cdp.eval("window.__fail.models=0; 'ok'")

            # ---- 空列表**不算成功**（最容易做成"静默成功"的一处）----
            cdp.eval("window.__calls=[]; 'ok'")
            picked = select_source(cdp, "mxgpt")
            check("⑦ 前置：重新选中源 `mxgpt`", picked, "点不中（列表状态被上一条判据改掉了）",
                  "⑦模型按钮")
            time.sleep(1.2)
            cdp.eval("document.getElementById('ps-fetch-models').click()")
            time.sleep(1.5)
            empty_models = cdp.eval("(document.getElementById('ps-available-hint')||{}).innerText||''") or ""
            check("⑦ 端点返回**空列表时不算成功**（如实警告，不静默当 OK）",
                  "0 个模型" in empty_models, f"got {empty_models!r}", "⑦模型按钮")

            # ---- 裸对象兼容（真后端可能不用 AstrBot 信封）----
            # 🔴 这条防的是最贵的静默失败：只认一种形状 → 另一种读成 undefined
            #    → 界面显示"还没有提供商源"，看着像没数据，其实是没读懂。
            cdp.eval(
                "(function(){var orig=window.fetch; window.fetch=function(i,init){"
                "  var u=(typeof i==='string')?i:(i&&i.url)||'';"
                "  if(u==='/api/v1/provider-sources'){"
                "    return Promise.resolve(new Response(JSON.stringify({"
                "      provider_sources:[{id:'bare',api_base:'https://bare.example/v1',"
                "        provider_type:'chat_completion',timeout:30,proxy:'',custom_headers:{},"
                "        key_configured:false,enable:true}]}),"
                "      {status:200,headers:{'Content-Type':'application/json'}}));}"
                "  return orig.apply(this,arguments);}; return 'ok';})()"
            )
            cdp.eval("applyRoute(); 'ok'")
            time.sleep(1.2)
            bare = cdp.eval(
                "(function(){var ul=document.getElementById('provider-source-list');"
                " var li=ul?ul.querySelector('li'):null;"
                " return {items: ul?ul.querySelectorAll('li').length:0,"
                "         id:(li&&li.querySelector('.src-id')||{}).innerText||'',"
                "         hint:(document.getElementById('providers-hint')||{}).innerText||''};})()"
            ) or {}
            check("⑧ 兼容**裸对象**响应（真后端可能不用 AstrBot 信封）—— 不静默变空列表",
                  bare.get("items") == 1 and bare.get("id") == "bare",
                  f"got {bare}（若为 0 项说明只认一种响应形状 = 静默失败）", "⑧错误如实")

            # ---- 形状不对必须**响亮报错**（不是静默当空）----
            cdp.eval(
                "(function(){var orig=window.fetch; window.fetch=function(i,init){"
                "  var u=(typeof i==='string')?i:(i&&i.url)||'';"
                "  if(u==='/api/v1/provider-sources'){"
                "    return Promise.resolve(new Response(JSON.stringify({status:'ok',data:{"
                "      provider_sources:'oops-not-an-array'}}),"
                "      {status:200,headers:{'Content-Type':'application/json'}}));}"
                "  return orig.apply(this,arguments);}; return 'ok';})()"
            )
            cdp.eval("applyRoute(); 'ok'")
            time.sleep(1.2)
            bad_shape = cdp.eval("(document.getElementById('providers-hint')||{}).innerText||''") or ""
            check("⑧ 响应形状不对时**响亮报错**（不是静默当成空列表）",
                  "失败" in bad_shape and ("不是数组" in bad_shape or "形状" in bad_shape),
                  f"got {bad_shape!r}", "⑧错误如实")

            # ---- 🔴 深链 + 列表读取失败：**必须报真根因，不许被"找不到该源"盖掉** ----
            # 守的是这条真实缺陷：`#/providers/<id>` 分支里
            #   `try { await loadProviderSources(); } catch(e) {}`
            # 而 `loadProviderSources()` 失败时是 **return false、不抛异常**
            #   → catch 永远不进 → 列表仍是空的 → 紧接着 `selectProviderSource(id)`
            #   找不到源 → 弹「找不到提供商源「syuan」。」
            #   → **用户以为"这个源不存在"，真相是"后端 500 / 形状不符"**。
            # 这正是本项目最贵的 bug 类型：真实故障被一句无关文案盖住。
            cdp.eval(FAKE_BACKEND_JS + "; 'fake-installed-again'")   # 覆盖掉上面的坏形状桩
            cdp.eval("window.__fail.list = 500; location.hash = '#/providers'; 'ok'")
            time.sleep(1.0)
            cdp.eval("location.hash = '#/providers/syuan'; 'ok'")
            time.sleep(1.8)
            mask = cdp.eval("(document.getElementById('providers-hint')||{}).innerText||''") or ""
            check("⑧ 🔴 深链时列表读取失败 → 面板报**真根因**（500 失败），"
                  "不许用「找不到提供商源」把根因盖掉",
                  ("失败" in mask) and ("500" in mask) and ("找不到提供商源" not in mask),
                  f"hint={mask!r}", "⑧错误如实")
            cdp.eval("window.__fail.list = 0; location.hash = '#/providers'; 'ok'")
            time.sleep(1.0)

            # ---- 深链：直接打开 #/providers/<源id>（刷新/分享）----
            # 🔴 必须**真刷新**，不能只 `Page.navigate` 到带 hash 的 URL：
            #    当前页已经在 `#/providers`，只改 fragment = **同文档导航**
            #    → 不重新加载、不跑 `boot()`、**刚注入的坏形状 fetch 还在**
            #    → 取数必然失败；判据测的就成了"我的注入还在不在"，与深链无关。
            #    （本项目踩过的老坑：截图脚本只改 hash 导航 → 断言全错。）
            cdp.eval("location.hash = '#/providers/syuan'; 'ok'")
            time.sleep(0.6)
            cdp.send("Page.reload")
            time.sleep(3.5)
            deep = cdp.eval(
                "(function(){var t=document.getElementById('provider-source-title');"
                " var g=function(i){var e=document.getElementById(i);return e?e.value:null;};"
                " return {hash:location.hash, title:t?t.innerText:'', id:g('ps-id'),"
                "         key:g('ps-api-key'),"
                "         hint:(document.getElementById('providers-hint')||{}).innerText||''};})()"
            ) or {}
            # 刷新后假后端已失效 → 走真后端；真后端没有 syuan 时**必须如实报错**，
            # 而不是白屏或假装选中（这才是这条判据要守的东西）。
            if backend_ok:
                check("④ 深链 `#/providers/<id>` 刷新后仍能落地（源存在时预填）",
                      deep.get("id") == "syuan" or "找不到" in (deep.get("hint") or ""),
                      f"got {deep}", "④选中加载")
            else:
                hint_now = deep.get("hint") or ""
                check("④ 深链在真后端未就绪时**如实报错**（不是白屏/不是假装选中）",
                      ("失败" in hint_now) or ("找不到" in hint_now), f"hint={hint_now!r}",
                      "④选中加载")

            # ---- 页面全程没有 JS 异常 ----
            errs = cdp.eval("JSON.stringify(window.__jsErrors||[])") or "[]"
            check("全程没有未捕获的 JS 异常（界面正常往往掩盖着抛错）",
                  json.loads(errs) == [], f"errors={errs}", "JS异常")

            shot = cdp.send("Page.captureScreenshot", format="png")
            if shot.get("data"):
                import base64
                Path("/tmp/provider_panel.png").write_bytes(base64.b64decode(shot["data"]))
                print("  · 截图 → /tmp/provider_panel.png")
            cdp.close()
        finally:
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print(f"\n{'=' * 68}")
    if failures:
        print(f"❌ {len(failures)}/{checks} 条判据失败：")
        for f in failures:
            print(f"   - {f}")
    else:
        print(f"✅ 全部 {checks} 条判据通过")
    if phase_fail:
        print("按阶段：" + "，".join(f"{k}={v}" for k, v in phase_fail.items()))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
