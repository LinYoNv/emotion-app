#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用**真实 Chrome**（CDP）验插件页的两个修复 + 卡片动作面。

## 这个脚本守什么（两条都是情绪 2026-10-31 报的）

### A. 「点插件独立 web 页后，列表整块还在」（他附的第三张截图红框）
判据 **A1**：进 `#/plugins/<id>/<page>` 之后，`#plugins-list-view` 必须
`hidden` **且高度为 0**。

🔴 这条是**反例判据**：修复前它必红。为什么不能只断言"元素存在" ——
   那个 bug 的形态就是"元素存在且可见"。也不能只断言 class 里有没有 `hidden`：
   修复前「插件升级」那块**根本没有被任何代码 toggle**，class 里根本没有 `hidden`
   ——所以才要断言"整块容器 hidden"，即**新的隐藏机制真的生效了**。

### B. 「插件的删除键没了」
判据 **B1**：卡片上有 `⋯`，点开菜单**恰好 7 条**，顺序与契约 §6.2 一致。
判据 **B2**：点「重载」**真的发出 `POST .../reload`**（fetch 钩子抓，不看 DOM）。
判据 **B3**：点「删除插件」→ 确认框出现 → **点取消后一个请求都不发**。
        （误点就直接删用户插件源码，是这台机器上代价最高的误操作。）

### C. 防回归 2026-09-27 那次事故
判据 **C1**：窄视口 900×470 下 `#plugin-page-list` 高度 > 0。
        （那次是"入口列表被压成 0 高、接口全绿而用户什么都看不见"。）

用法：
    .venv/bin/python tools/plugin_actions_ui_check.py <base_url> <token>
退出码 0 = 全部通过。**没有"跳过"** —— 前置不满足就是失败。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9227


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=120)
        self._id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 失败: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str, *, await_promise: bool = False):
        res = self.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if res.get("exceptionDetails"):
            raise RuntimeError(f"JS 异常: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _launch() -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--remote-debugging-port={PORT}",
            # Chrome 111+ 拒绝带 Origin 的 CDP 连接（403），必须显式放开
            "--remote-allow-origins=*",
            "--window-size=1280,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            _http_json("/json/version")
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Chrome 起不来")


#: 等"页面静止"的探针：`__fetchSeq` 计数不再增长 ⇒ 在飞请求归零。
#: 🔴 不用固定 `sleep` —— 本项目已记：判据的前置必须等异步稳定。
QUIET = r"""
(() => {
  const s = window.__fetchSeq || 0;
  window.__fetchSeqAtQuiet = s;
  return s;
})()
"""


def _wait_quiet(cdp: CDP, *, timeout: float = 15.0) -> int:
    """等到 fetch 序号不再变化，返回最终序号。"""
    deadline = time.monotonic() + timeout
    last = -1
    stable_since = None
    while time.monotonic() < deadline:
        seq = cdp.eval("window.__fetchSeq || 0")
        if seq == last:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 0.6:   # 连续 0.6s 不动
                return seq
        else:
            last = seq
            stable_since = None
        time.sleep(0.1)
    raise RuntimeError(f"页面在 {timeout}s 内没有静止（最后序号 {last}）")


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: plugin_actions_ui_check.py <base_url> <token>")
        return 2
    base = sys.argv[1].rstrip("/") + "/"
    token = sys.argv[2]

    fails: list[str] = []
    oks: list[str] = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        (oks if cond else fails).append(label + (f" — {detail}" if detail else ""))
        print(("  ✅ " if cond else "  ❌ ") + label + (f" — {detail}" if detail else ""))

    proc = _launch()
    cdp = None
    try:
        tabs = _http_json("/json/list")
        page = next(t for t in tabs if t["type"] == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        # ---- 装 fetch 钩子（必须在导航前注入，否则漏掉首屏请求）----
        cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    window.__fetchSeq = 0;
                    window.__calls = [];
                    const _f = window.fetch;
                    window.fetch = function(...a) {
                      const url = (a[0] && a[0].url) || String(a[0]);
                      const opt = a[1] || {};
                      window.__fetchSeq += 1;
                      window.__calls.push({
                        url: String(url),
                        method: String(opt.method || 'GET').toUpperCase(),
                        body: typeof opt.body === 'string' ? opt.body.slice(0, 400) : null,
                      });
                      return _f.apply(this, a);
                    };
                """
            },
        )

        # ---- 登录（走真登录接口拿 cookie，不手工塞头）----
        cdp.send("Page.navigate", {"url": base})
        time.sleep(1.5)
        login = cdp.eval(
            """
            (async () => {
              const r = await fetch('/api/v1/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({token: %s}),
                credentials: 'same-origin',
              });
              return r.status;
            })()
            """
            % json.dumps(token),
            await_promise=True,
        )
        check(login == 200, "登录成功（拿到会话 cookie）", f"status={login}")
        if login != 200:
            raise SystemExit(1)

        # 🔴 **必须显式关掉登录浮层**（2026-10-31 实测踩到）：
        #    我们是走 `fetch` 登录的，**没有经过** `#login-form` 的 submit ——
        #    而 `boot()`（真正 hideLogin 的地方）只由两条路径触发：
        #    ① 表单提交成功 ② 页面加载时 `isLoggedIn()` 为真。
        #    这次导航发生在登录**之前**，所以那一轮判定是"没登录" ⇒ 浮层一直开着，
        #    并且它 `position:fixed; inset:0` 铺满全屏。
        #    后果：`elementFromPoint` **永远命中浮层**，任何"真机点得动"的判据
        #    都会假红（我第一次跑就是 hits=0/7，差点误判成产品把菜单裁掉了）。
        cdp.send("Page.reload")
        _wait_quiet(cdp)
        hidden = cdp.eval(
            "(() => { const l = document.getElementById('login');"
            " if (!l) return 'no-el'; l.classList.add('hidden');"
            " return l.classList.contains('hidden') ? 'hidden' : 'still-shown'; })()"
        )
        check(hidden == "hidden", "登录浮层已关闭（否则命中测试一律假红）",
              f"{hidden}")

        # ---- 拿一个真有拓展页的插件（判据要真进它的独立页）----
        cdp.send("Page.navigate", {"url": base + "#/plugins"})
        _wait_quiet(cdp)
        pages = cdp.eval(
            """
            (async () => {
              const r = await fetch('/api/v1/plugins/pages', {credentials:'same-origin'});
              const d = await r.json();
              return JSON.stringify(d.pages || []);
            })()
            """,
            await_promise=True,
        )
        page_list = json.loads(pages) if pages else []
        check(len(page_list) > 0, "有插件拓展页可测（否则 A1 无法验证）",
              f"{len(page_list)} 个")
        if not page_list:
            raise SystemExit(1)
        target = page_list[0]
        plugin_id, page_name = target["plugin_id"], target["page_name"]

        # ---- ① 进插件独立页 → 列表态必须隐藏 ----
        print("\n===== ① 插件独立页里列表态必须隐藏（任务 A 的反例断言） =====")
        cdp.eval(
            "location.hash = '#/plugins/%s/%s'"
            % (plugin_id, page_name)
        )
        _wait_quiet(cdp)
        time.sleep(1.0)     # 等 iframe 开始加载（它自己的请求不归我们的 fetch 计）

        state = json.loads(
            cdp.eval(
                """
                (() => {
                  const el = document.getElementById('plugins-list-view');
                  const frame = document.getElementById('plugin-frame-wrap');
                  if (!el) return JSON.stringify({missing: true});
                  const r = el.getBoundingClientRect();
                  return JSON.stringify({
                    hidden: el.classList.contains('hidden'),
                    h: Math.round(r.height),
                    display: getComputedStyle(el).display,
                    frameHidden: frame ? frame.classList.contains('hidden') : null,
                    switchListH: (() => {
                      const s = document.getElementById('plugin-switch-list');
                      return s ? Math.round(s.getBoundingClientRect().height) : null;
                    })(),
                    updateListH: (() => {
                      const u = document.getElementById('plugin-update-list');
                      return u ? Math.round(u.getBoundingClientRect().height) : null;
                    })(),
                  });
                })()
                """
            )
        )
        check(not state.get("missing"), "A0 `#plugins-list-view` 元素存在")
        # 🔴 这条就是修复前必红的那条
        check(
            state.get("hidden") is True and state.get("h", 1) == 0,
            "A1 进插件独立页后，列表整块（启停+升级+卡片+拓展页）已隐藏",
            f"hidden={state.get('hidden')} h={state.get('h')} "
            f"switchListH={state.get('switchListH')} updateListH={state.get('updateListH')}",
        )
        check(state.get("frameHidden") is False, "A2 插件 iframe 处于显示态")

        # ================= A3：回列表，整块必须回来 =================
        cdp.eval("location.hash = '#/plugins'")
        _wait_quiet(cdp)
        back = json.loads(
            cdp.eval(
                """
                (() => {
                  const el = document.getElementById('plugins-list-view');
                  const r = el.getBoundingClientRect();
                  return JSON.stringify({
                    hidden: el.classList.contains('hidden'),
                    h: Math.round(r.height),
                  });
                })()
                """
            )
        )
        check(
            back.get("hidden") is False and back.get("h", 0) > 0,
            "A3 回列表后整块恢复可见且有高度",
            f"hidden={back.get('hidden')} h={back.get('h')}",
        )

        # ================= B1：⋯ 菜单 =================
        menu = json.loads(
            cdp.eval(
                """
                (() => {
                  const triggers = document.querySelectorAll('.plugin-menu-trigger');
                  if (!triggers.length) return JSON.stringify({triggers: 0});
                  triggers[0].click();
                  const open = document.querySelectorAll('.plugin-menu:not(.hidden)');
                  const first = open[0];
                  const items = first
                    ? [...first.querySelectorAll('.plugin-menu-item')].map(b => b.textContent)
                    : [];
                  const expanded = triggers[0].getAttribute('aria-expanded');
                  // 再点第二个触发器：同一时刻只能开一个
                  let overlap = null;
                  if (triggers[1]) {
                    triggers[1].click();
                    overlap = document.querySelectorAll('.plugin-menu:not(.hidden)').length;
                    triggers[1].click();   // 收起
                  }
                  return JSON.stringify({triggers: triggers.length, items, expanded, overlap});
                })()
                """
            )
        )
        check(menu.get("triggers", 0) > 0, "B0 卡片上有「⋯」按钮",
              f"{menu.get('triggers')} 个")
        EXPECTED = ["行为", "README", "插件设置", "重载", "重新安装",
                    "更换插件源…", "删除插件"]
        check(menu.get("items") == EXPECTED, "B1 菜单 7 条且顺序符合契约 §6.2",
              f"{menu.get('items')}")
        check(menu.get("expanded") == "true", "B2 `aria-expanded` 同步为 true")
        check(menu.get("overlap") == 1, "B3 同一时刻只允许一个菜单打开",
              f"overlap={menu.get('overlap')}")

        # ---- B3b 🔴 菜单必须**真的点得到**（2026-10-31 本条是假绿抓出来的）----
        # 老版本只做 `b.click()` —— 那是**程序化点击，绕过命中测试**。
        # 实测当时 `.plugin-card{overflow:hidden}` 把 `bottom:100%` 的菜单
        # 整个裁掉（菜单顶沿比卡片上沿高 160px），7 个条目 elementFromPoint
        # 全部命中到别的元素，**真机上一个都点不动**，而脚本全绿。
        # ⇒ 判据必须验几何 + 命中：矩形在视口内、且每一条的中心点命中的是自己。
        hit = json.loads(
            cdp.eval(
                """
                (() => {
                  const card = document.querySelector('#plugin-update-list .plugin-card');
                  if (!card) return JSON.stringify({noCard: true});
                  card.scrollIntoView({block: 'center'});
                  const t = card.querySelector('.plugin-menu-trigger');
                  if (t.getAttribute('aria-expanded') !== 'true') t.click();
                  const menu = card.querySelector('.plugin-menu');
                  const items = [...menu.querySelectorAll('.plugin-menu-item')];
                  const hits = items.map(b => {
                    const r = b.getBoundingClientRect();
                    const e = document.elementFromPoint(
                      r.left + r.width / 2, r.top + r.height / 2);
                    return !!(e && (e === b || b.contains(e) || e.contains(b)));
                  });
                  const mr = menu.getBoundingClientRect();
                  return JSON.stringify({
                    cardOverflow: getComputedStyle(card).overflow,
                    hits: hits,
                    labeled: items.map(b => b.textContent.trim()),
                    menuInViewport: mr.top >= 0 && mr.left >= 0
                      && mr.bottom <= window.innerHeight
                      && mr.right <= window.innerWidth,
                    menuRect: {top: Math.round(mr.top), bottom: Math.round(mr.bottom)},
                  });
                })()
                """
            )
        )
        check(hit.get("menuInViewport") is True,
              "B3b 菜单整体在视口内（未被视口切掉）",
              f"menuRect={hit.get('menuRect')}")
        check(hit.get("cardOverflow") != "hidden",
              "B3c 卡片没有 overflow:hidden（否则会裁掉向上弹出的菜单）",
              f"card.overflow={hit.get('cardOverflow')}")
        _hits = hit.get("hits") or []
        check(len(_hits) == 7 and all(_hits),
              "B3d 🔴 7 个菜单条目**中心点都命中的是自己**（真机点得动）",
              f"hits={_hits} 命中={sum(1 for x in _hits if x)}/{len(_hits)}")
        cdp.eval(
            "document.dispatchEvent(new KeyboardEvent('keydown',"
            " {key:'Escape', bubbles:true}));"
        )

        # ================= B4：Esc 关闭 =================
        cdp.eval(
            """
            (() => {
              const t = document.querySelector('.plugin-menu-trigger');
              if (t && t.getAttribute('aria-expanded') !== 'true') t.click();
              document.dispatchEvent(new KeyboardEvent('keydown',
                {key: 'Escape', bubbles: true}));
            })()
            """
        )
        time.sleep(0.2)
        esc = json.loads(
            cdp.eval(
                """
                (() => JSON.stringify({
                  open: document.querySelectorAll('.plugin-menu:not(.hidden)').length,
                  expanded: (document.querySelector('.plugin-menu-trigger') || {})
                    .getAttribute && document.querySelector('.plugin-menu-trigger')
                    .getAttribute('aria-expanded'),
                }))()
                """
            )
        )
        check(esc.get("open") == 0 and esc.get("expanded") == "false",
              "B4 按 Esc 能关菜单且 aria-expanded 复位",
              f"open={esc.get('open')} expanded={esc.get('expanded')}")

        # ================= B5：点「重载」真的发请求 =================
        # 🔴 这里必须**拦住真实网络**：真 `POST .../reload` 会 terminate 掉正在跑的
        #    插件实例再重新 import —— 在这台机器上是破坏性操作，而且会让后面的
        #    判据跑在一个刚被拆过的宿主上。判据要验的是"**前端有没有发出**"，
        #    不是"后端做没做"（后端那部分有自己的 pytest 判据）。
        #    ⚠️ 老版本**没拦**，实测真的把 `content_companion` 重载了一次。
        cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": """
            (function () {
              // 计数器（上面的钩子已被本脚本换掉，这里一并重建）
              window.__fetchSeq = window.__fetchSeq || 0;
              window.__calls = window.__calls || [];
              const origFetch = window.fetch;
              window.fetch = function (input, init) {
                const url = typeof input === 'string' ? input : (input && input.url) || '';
                const method = ((init && init.method) || 'GET').toUpperCase();
                window.__fetchSeq += 1;
                // 🔴🔴 **reload 不拦截，DELETE 才拦截**（2026-10-31 修）。
                //
                //    第一版把 `reload` 也拦了并返回**自己编的**响应
                //    （`result: {handler_count: 0, ...}`）⇒ 判据显示 `handlers=0`，
                //    我据此以为"重载把 handler 弄丢了"，查了一轮才发现是脚本编的 0。
                //    更要紧的是：那样 B5/B6 **只**证明"前端发了请求"，
                //    "重载链路通不通 / 后端返回的计数对不对"**一条都没验到**。
                //
                //    现在：reload 走**真实链路**（在本机隔离实例上安全 ——
                //    只是 terminate + re-import 一个插件，几秒恢复，
                //    B6a 会断言真实的 handlers>0）；
                //    DELETE 仍拦截（**真删用户插件源码不可接受**），
                //    而删除判据只验"取消时没发请求"+"确认文案齐全"，拦截不影响其有效性。
                const isDelete = /\\/api\\/v1\\/plugins\\/[^/]+$/.test(url) && method === 'DELETE';
                const dangerous = isDelete;
                if (dangerous) {
                  // 🔴 **先记下来再拦**：判据要验"前端发了没有"。
                  //    老版本这里直接 return 了，`__calls` 里什么都没有 ⇒
                  //    判据 B5 报"calls=0"，而产品其实**发对了**（假红）。
                  window.__calls.push({url: String(url), method: method,
                                       body: typeof (init && init.body) === 'string'
                                         ? String(init.body) : null,
                                       intercepted: true});
                  return Promise.resolve(new Response(JSON.stringify({
                    ok: true, message: '（判据拦截，未真执行）',
                    result: {handler_count: 0, llm_tool_bind_count: 0, web_api_count: 0},
                    trash_path: '/tmp/x', moved_by: 'rename', removed: {},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}}));
                }
                return origFetch.apply(this, arguments);
              };
            })();
        """})
        # 重新加载页面让拦截钩子生效（钩子只对新文档生效）
        cdp.send("Page.reload")
        _wait_quiet(cdp)
        # 先挑一个 can_reload 的卡片（用后端字段，不猜）
        reload_res = json.loads(
            cdp.eval(
                """
                (async () => {
                  window.__calls.length = 0;
                  const card = document.querySelector('.plugin-card');
                  if (!card) return JSON.stringify({noCard: true});
                  card.querySelector('.plugin-menu-trigger').click();
                  const menu = card.querySelector('.plugin-menu');
                  const btn = [...menu.querySelectorAll('.plugin-menu-item')]
                    .find(b => b.textContent === '重载');
                  if (!btn) return JSON.stringify({noReloadItem: true});
                  if (btn.disabled) return JSON.stringify({disabled: btn.title || ''});
                  btn.click();
                  await new Promise(r => setTimeout(r, 3000));
                  const calls = window.__calls.filter(c =>
                    c.url.includes('/reload') && c.method === 'POST');
                  return JSON.stringify({
                    reloadCalls: calls.length,
                    url: calls[0] && calls[0].url,
                    result: (document.getElementById('plugin-update-result')||{}).textContent,
                  });
                })()
                """,
                await_promise=True,
            )
        )
        if reload_res.get("disabled"):
            # 所有插件都被停用 → 如实报失败（**不许跳过**）
            check(False, "B5 点「重载」真的发出 POST .../reload",
                  f"不可用：{reload_res['disabled']}")
        else:
            check(reload_res.get("reloadCalls", 0) >= 1,
                  "B5 点「重载」真的发出 POST .../reload",
                  f"calls={reload_res.get('reloadCalls')} url={reload_res.get('url')}")
            # 🔴 B6a 是防"stub 编数据"那类假绿的**硬断言**：
            #    只要后端真跑过，content_companion 有 2 个 handler，绝不可能 0。
            shown = reload_res.get("result") or ""
            m = re.search(r"handlers=(\d+)", shown)
            check(bool(m) and int(m.group(1)) > 0,
                  "B6a 重载后的 handlers 是**真实数字**（>0，不是 stub 编的 0）",
                  f"shown={shown[:60]!r}")
            check("不需要重启" in shown,
                  "B6 重载后的提示写明「不需要重启」",
                  repr((reload_res.get("result") or "")[:80]))

        # ================= B7：删除的取消不发请求 =================
        # 🔴 用 CDP 拦 `window.confirm` 返回 false（**不真删**用户插件）
        cancel_res = json.loads(
            cdp.eval(
                """
                (async () => {
                  window.__calls.length = 0;
                  const orig = window.confirm;
                  let called = 0;
                  let seen = '';
                  window.confirm = (msg) => {
                    called += 1;
                    seen = String(msg || '');
                    return false;                    // 模拟用户点"取消"
                  };
                  const card = document.querySelector('.plugin-card');
                  card.querySelector('.plugin-menu-trigger').click();
                  const btn = [...card.querySelectorAll('.plugin-menu-item')]
                    .find(b => b.textContent === '删除插件');
                  if (!btn) { window.confirm = orig; return JSON.stringify({noItem:true}); }
                  if (btn.disabled) {
                    window.confirm = orig;
                    return JSON.stringify({disabled: btn.title || ''});
                  }
                  btn.click();
                  await new Promise(r => setTimeout(r, 600));
                  const dels = window.__calls.filter(c => c.method === 'DELETE');
                  window.confirm = orig;
                  return JSON.stringify({
                    confirmCalled: called,
                    confirmText: seen,
                    deleteCalls: dels.length,
                  });
                })()
                """,
                await_promise=True,
            )
        )
        if cancel_res.get("noItem"):
            check(False, "B7 点「删除插件」弹确认框、取消后不发请求", "菜单项不存在")
        elif cancel_res.get("disabled"):
            check(False, "B7 点「删除插件」弹确认框、取消后不发请求",
                  f"不可用：{cancel_res['disabled']}")
        else:
            check(cancel_res.get("confirmCalled", 0) == 1,
                  "B7a 点「删除插件」确实弹了二次确认",
                  f"called={cancel_res.get('confirmCalled')}")
            check(cancel_res.get("deleteCalls", 1) == 0,
                  "B7b 取消后**一个 DELETE 请求都不发**",
                  f"deleteCalls={cancel_res.get('deleteCalls')}")
            # 契约 §3 / §6.2 的四件必写项，逐条查（文案写了但漏一项 = 用户判断不了）
            _t = cancel_res.get("confirmText") or ""
            _need = {
                "源码目录": "/plugins/" in _t or "源码目录" in _t,
                "回收站位置": "回收站" in _t,
                "数据默认不删": "不会删除" in _t or "不删" in _t,
                "无永久删除": "永久删除" in _t,
            }
            check(all(_need.values()),
                  "B7c 确认文案含 源码目录 / 回收站 / 数据不删 / 无永久删除",
                  f"逐项={_need}｜文案={_t[:140]!r}")
            # 🔴 删除条目必须**红色**：读 computed color，不认类名
            #    （改类名不改颜色、或 CSS 被覆盖时，类名判据会假绿）
            red = cdp.eval(
                """
                (() => {
                  const card = document.querySelector('.plugin-card');
                  card.querySelector('.plugin-menu-trigger').click();
                  const b = [...card.querySelectorAll('.plugin-menu-item')]
                    .find(x => x.textContent === '删除插件');
                  return b ? getComputedStyle(b).color : null;
                })()
                """
            )
            # --danger = #ec7a8b = rgb(236, 122, 139)
            check(red == "rgb(236, 122, 139)",
                  "B7d 「删除插件」条目是 danger 红（读 computed color）",
                  f"color={red}")
            cdp.eval(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                " {key:'Escape', bubbles:true}));"
            )

        # ================= C1：窄视口下入口列表不被压成 0 =================
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": 900, "height": 470, "deviceScaleFactor": 1, "mobile": False,
        })
        cdp.eval("location.hash = '#/plugins'")
        _wait_quiet(cdp)
        narrow = json.loads(
            cdp.eval(
                """
                (() => {
                  const ul = document.getElementById('plugin-page-list');
                  const sw = document.getElementById('plugin-switch-list');
                  const r = ul ? ul.getBoundingClientRect() : {height: 0};
                  return JSON.stringify({
                    listH: Math.round(r.height),
                    switchH: sw ? Math.round(sw.getBoundingClientRect().height) : null,
                    rows: document.querySelectorAll('#plugin-switch-list .switch-row').length,
                  });
                })()
                """
            )
        )
        check(narrow.get("listH", 0) > 0,
              "C1 窄视口 900×470 下 `#plugin-page-list` 高度 > 0（防 2026-09-27 回归）",
              f"h={narrow.get('listH')} switchH={narrow.get('switchH')} "
              f"rows={narrow.get('rows')}")
        cdp.send("Emulation.clearDeviceMetricsOverride")

    finally:
        if cdp is not None:
            cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print()
    print(f"通过 {len(oks)} 条，失败 {len(fails)} 条")
    if fails:
        print("\n失败明细：")
        for f in fails:
            print("  ❌ " + f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
