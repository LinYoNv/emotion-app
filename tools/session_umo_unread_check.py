#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真浏览器判据：会话页的 **umo 标签** + 侧边栏「聊天」**未读徽标**。

## 为什么必须真浏览器

情绪 2026-09-26 要的两件事都**发生在 DOM 上**，`curl` 证明不了：
  * 「每个会话都带一个唯一标签 umo」—— 值存在接口里 ≠ 用户**看得见、复制得走**；
  * 「给"聊天"加一个功能：用户没有阅读的时候出个提示，就像"插件"旁边那个圆圈 3」
    —— 徽标显不显示、什么时候消失，全是 JS 行为。

⚠️ 本项目的老坑（必须靠真浏览器才发现）：
  * id 落错容器（设到 `<span>` 而不是 `<select>`）→ 取值 `undefined`，接口测试全绿；
  * 「已上传」文案把持久状态**盖掉**，用户传完反而看不到当前用的是哪张图。

## 判据（每一条都能失败）

  ① 聊天 nav 上**有**徽标这个元素（结构与「插件」那个一致）
  ② 没有未读时徽标**是隐藏的**（不能常亮 —— 常亮等于没有提示）
  ③ 会话页有「复制 umo」「换一个专用 umo」两个控件
  ④ 选中会话时，完整 umo **真显示出来**（`11:FriendMessage:<tag>`）
  ⑤ 新建会话 → 面板上显示的 umo 与话术**逐字对得上**（同一个 tag）
  ⑥ 用真 UI 点「换一个专用 umo」→ 值真的变了、提示真的更新了
  ⑦ 造一条主动消息（走真投递端口）→ 徽标**出现**且数字 = 1
  ⑧ 打开该会话（真 UI 路由）→ 徽标**消失**（这验证"读了就消"）
  ⑨ 全程零 JS 报错（`Runtime.exceptionThrown` 计数为 0）

用法：python3 tools/session_umo_unread_check.py <base_url> <token>
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
import uuid

import websocket  # type: ignore

CHROME = "/usr/bin/google-chrome"
PORT = 9228


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self._id = 0
        #: JS 报错计数（照 `Runtime.exceptionThrown`）——判据⑨要用
        self.exceptions: list[str] = []

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.exceptionThrown":
                d = (msg.get("params") or {}).get("exceptionDetails") or {}
                self.exceptions.append(str(d.get("text") or d)[:200])
                continue
            if msg.get("id") == self._id:
                return msg.get("result", {})

    def eval(self, expr: str, *, await_promise: bool = False):
        r = self.send("Runtime.evaluate", expression=expr,
                      awaitPromise=await_promise, returnByValue=True)
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _deliver_one_proactive(*, session_id: str, umo: str) -> dict:
    """从判据进程调用**真的投递端口**发一条主动消息（跨进程写同一个库）。

    🔴 为什么允许跨进程：`SessionDelivery.send()` 是这个平台**唯一**的
       "插件主动消息落地"入口，插件的两条投递路径最终都走到它。
       在本进程里调它 = 跑的就是那条代码路径；写进的是服务端同一个
       sqlite 文件，而读取端每次请求都现读（`load_sections`，无缓存）。
       ⚠️ 与 `tests/test_session_umo_unread.py::test_delivery_port_marks_unread`
          的区别：那条在**同进程**验「投递 → 未读」，本条验
          「未读 → 服务端接口 → 浏览器 DOM」；两段合起来才是整链。
    """
    import asyncio
    import sys as _sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent
    for p in (str(root), str(root / "host")):
        if p not in _sys.path:
            _sys.path.insert(0, p)
    try:
        from emotion_app.storage.sqlite_store import SqliteStore
        from emotion_app.web.delivery import SessionDelivery

        db = _storage_path(root)

        class _Chain:
            def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
                return "（判据）我想你了"

        async def _run() -> dict:
            store = SqliteStore(db)
            await store.open()
            try:
                #: ⚠️ 传 `sessions=` 一个**真仓库**；`settings=None` 是合法的
                #:    （投递端口对它可以缺席 —— 只影响新会话挂不挂人设）
                from emotion_app.domain.session_repo import SessionRepository

                delivery = SessionDelivery(sessions=SessionRepository(store), settings=None)
                ok = await delivery.send(umo, _Chain())
                s = await SessionRepository(store).get(session_id)
                return {"ok": ok is True, "unread": s.unread, "db": str(db)}
            finally:
                await store.close()

        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — 判据要如实报错，不静默当失败
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _storage_path(root) -> str:
    """取服务端在用的 sqlite 路径（**从 `.env` 读**，与运行中的服务同源）。"""
    import re as _re
    from pathlib import Path as _Path

    env = _Path(root) / ".env"
    if env.is_file():
        m = _re.search(r"^EMOTION_STORAGE_SQLITE_PATH=(.*)$",
                       env.read_text(encoding="utf-8"), _re.M)
        if m:
            val = m.group(1).strip().strip("'\"")
            if val:
                return str((_Path(root) / val).resolve())
    return str(_Path(root) / "data" / "emotion.db")


def main() -> int:
    if len(sys.argv) < 3:
        print("用法：python3 tools/session_umo_unread_check.py <base_url> <token>")
        return 2
    base, token = sys.argv[1].rstrip("/"), sys.argv[2]

    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--remote-allow-origins=*",
         "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    results: list[tuple[str, bool, str]] = []
    made: list[str] = []
    try:
        for _ in range(40):
            try:
                _http_json("/json/version")
                break
            except Exception:
                time.sleep(0.5)
        tabs = _http_json("/json/list")
        page = [t for t in tabs if t["type"] == "page"][0]
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        # ---- 登录（走真表单，不绕过前端逻辑）----
        cdp.send("Page.navigate", url=f"{base}/")
        time.sleep(2.5)
        cdp.eval(
            "document.getElementById('login-token').value=" + json.dumps(token) + ";"
            "document.getElementById('login-form').dispatchEvent("
            "new Event('submit',{cancelable:true}));"
        )
        time.sleep(2.5)

        # ---- ① 徽标元素存在（结构与「插件」那个一致）----
        badge = cdp.eval(
            """
            (() => {
              const el = document.getElementById('chat-badge');
              const nav = document.querySelector('.nav-item[data-nav="chat"]');
              return {
                exists: !!el,
                insideChatNav: !!(el && nav && nav.contains(el)),
                hasClass: !!(el && el.classList.contains('nav-badge')),
                text: el ? el.textContent : null,
                hidden: el ? el.classList.contains('hidden') : null,
              };
            })()
            """
        )
        results.append((
            "① 「聊天」nav 上有未读徽标（结构同「插件」）",
            bool(badge) and badge.get("exists") and badge.get("insideChatNav")
            and badge.get("hasClass"),
            f"{badge}",
        ))

        # ---- ② 无未读时必须隐藏（常亮 = 假提示）----
        # 先确保基线干净：把库里所有会话标记已读（这是**判据的前置**，要主动建立）
        cdp.eval(
            """
            (async () => {
              const d = await (await fetch('/api/v1/sessions')).json();
              for (const s of (d.data || [])) {
                await fetch('/api/v1/sessions/' + s.id + '/read', {method: 'POST'});
              }
              await refreshUnread();
              return true;
            })()
            """,
            await_promise=True,
        )
        time.sleep(0.6)
        clean = cdp.eval(
            "(() => { const el = document.getElementById('chat-badge');"
            " return {hidden: el.classList.contains('hidden'), text: el.textContent}; })()"
        )
        results.append((
            "② 无未读时徽标隐藏（不常亮）",
            bool(clean) and clean.get("hidden") is True,
            f"{clean}",
        ))

        # ---- ③ 会话页两个控件在 ----
        # 走真 UI 路径进「会话」页
        cdp.eval("location.hash = '#/session';")
        time.sleep(1.2)
        ctl = cdp.eval(
            """
            (() => ({
              copy: !!document.getElementById('s-umo-copy'),
              regenerate: !!document.getElementById('s-umo-new'),
              hint: !!document.getElementById('s-umo-hint'),
            }))()
            """
        )
        results.append((
            "③ 会话页有「复制 umo」「换一个专用 umo」控件",
            bool(ctl) and ctl.get("copy") and ctl.get("regenerate") and ctl.get("hint"),
            f"{ctl}",
        ))

        # ---- ④ 自建会话 → 面板显示的 umo 与后端一致 ----
        title = f"chk-umo-{uuid.uuid4().hex[:8]}"
        created = cdp.eval(
            """
            (async () => {
              const r = await fetch('/api/v1/sessions', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title: %s}),
              });
              const d = await r.json();
              return {status: r.status, id: d.id || null, umo: d.umo || '', user_id: d.user_id || ''};
            })()
            """
            % json.dumps(title),
            await_promise=True,
        )
        if created and created.get("id"):
            made.append(created["id"])
        results.append((
            "④ 新建会话自带 umo（接口层）",
            bool(created) and created.get("status") == 201
            and created.get("umo", "").startswith("11:FriendMessage:")
            and created.get("user_id") == created.get("umo", "").split(":", 2)[-1],
            f"{created}",
        ))

        # 走真 UI：进会话页并选中它（列表项点击）
        sel = cdp.eval(
            """
            (async () => {
              location.hash = '#/session';
              await new Promise(r => setTimeout(r, 300));
              await loadSessions();
              await selectSession(%s);
              await new Promise(r => setTimeout(r, 300));
              return {
                inputValue: document.getElementById('s-user-id').value,
                hint: document.getElementById('s-umo-hint').textContent,
              };
            })()
            """
            % json.dumps(created["id"]),
            await_promise=True,
        )
        shown_ok = bool(sel) and sel.get("inputValue") == created["user_id"] \
            and created["umo"] in (sel.get("hint") or "")
        results.append((
            "⑤ 选中会话后，完整 umo 在面板上**看得见**",
            shown_ok,
            f"输入框={sel and sel.get('inputValue')!r}；提示行={sel and sel.get('hint')!r}",
        ))

        # ---- ⑥ 真 UI 点「换一个专用 umo」----
        # ⚠️ 它有 confirm() —— headless 下 confirm 默认返回 false，会直接 return。
        #    判据里**覆盖 confirm**（不是绕过 UI：按钮的 onclick 仍然真跑）
        changed = cdp.eval(
            """
            (async () => {
              const before = document.getElementById('s-user-id').value;
              window.confirm = () => true;
              document.getElementById('s-umo-new').click();
              await new Promise(r => setTimeout(r, 900));
              return {
                before,
                after: document.getElementById('s-user-id').value,
                hint: document.getElementById('s-umo-hint').textContent,
              };
            })()
            """,
            await_promise=True,
        )
        results.append((
            "⑥ 点「换一个专用 umo」→ 值真的换了且提示更新",
            bool(changed) and changed.get("after") and changed["after"] != changed["before"]
            and changed["after"] in (changed.get("hint") or ""),
            f"{changed}",
        ))

        # ---- ⑦ 真投递端口 → 未读 → 徽标出现（**整条链都是真的**）----
        # 🔴 关键设计：**从判据进程调用真的 `SessionDelivery.send()`**，
        #    而不是"直接改库里的 unread 字段"。区别很大：
        #      * 直接改库只证明"前端会渲染 unread>0"，证明不了投递那一半；
        #      * 走真投递端口 = 插件给宿主的那条路（`context.send_message`
        #        / `platform.send_by_session` 最终都落到它）→ 它自己算
        #        `unread_delta=1` 并落盘。
        #    ⚠️ 写的是**服务端同一个 sqlite 文件**（`SessionRepository` 每次
        #       请求都 `load_sections` 现读，不缓存）→ 服务端**立刻**能看到。
        #        这里如实注明：跨进程而非同进程，但代码路径与落库完全相同。
        # 🔴 **必须现读当前 umo**，不能用 ④ 步那个：
        #    ⑥ 刚把标签换掉了（`sess-8f4e…` → `sess-79c3…`），拿旧值投递会
        #    落到"旧标签派生出的另一条会话"上 —— 红点出现在一条用户没打开的
        #    会话上，判据⑧ 就会误报成产品 bug。
        #    这正是本项目那条通则：「判据不许用上一轮自己造成的陈旧状态当证据」。
        fresh = cdp.eval(
            """
            (async () => {
              const r = await fetch('/api/v1/sessions/' + %s);
              const d = await r.json();
              return {umo: d.umo || '', user_id: d.user_id || ''};
            })()
            """
            % json.dumps(created["id"]),
            await_promise=True,
        )
        results.append((
            "⑦a 投递前现读当前 umo（不用 ④ 步的旧值）",
            bool(fresh) and fresh.get("umo", "").startswith("11:FriendMessage:")
            and fresh.get("user_id") not in ("", created.get("user_id")),
            f"{fresh}",
        ))
        delivered = _deliver_one_proactive(
            session_id=created["id"], umo=(fresh or {}).get("umo") or created["umo"]
        )
        results.append((
            "⑦ 走真投递端口发出主动消息（跨进程，同一个库）",
            bool(delivered) and delivered.get("ok") is True,
            f"{delivered}",
        ))

        # 浏览器轮询一次 → 徽标必须**出现**，且数字是 1
        appearance = cdp.eval(
            """
            (async () => {
              await refreshUnread();
              await new Promise(r => setTimeout(r, 200));
              const el = document.getElementById('chat-badge');
              return {
                hidden: el.classList.contains('hidden'),
                text: el.textContent,
                state: state.unreadTotal,
              };
            })()
            """,
            await_promise=True,
        )
        # 🔴 **落在哪条会话**也要验：落到派生会话（`plugin:…`）上同样是 bug ——
        #    用户永远不会去打开那条，红点就成了永久假提示。
        placement = cdp.eval(
            """
            (async () => {
              const d = await (await fetch('/api/v1/unread')).json();
              return {ids: Object.keys(d.sessions || {}), total: d.total};
            })()
            """,
            await_promise=True,
        )
        results.append((
            "⑦c 未读落在**用户那条**会话上（不是派生会话）",
            bool(placement) and placement.get("ids") == [created["id"]],
            f"期望 [{created['id']}]，实际 {placement}",
        ))

        results.append((
            "⑦b 徽标出现且数字 = 1（前端渲染的是真未读）",
            bool(appearance) and appearance.get("hidden") is False
            and appearance.get("text") == "1",
            f"{appearance}",
        ))

        # ---- ⑧ 真 UI 打开该会话 → 徽标消失（读完即消）----
        cleared = cdp.eval(
            """
            (async () => {
              // 真 UI 路径：切走再切回 → 触发 selectSession → markSessionRead
              location.hash = '#/chat';
              await new Promise(r => setTimeout(r, 300));
              location.hash = '#/session';
              await new Promise(r => setTimeout(r, 300));
              await loadSessions();
              await selectSession(%s);
              await new Promise(r => setTimeout(r, 600));
              await refreshUnread();
              const el = document.getElementById('chat-badge');
              return {
                hidden: el.classList.contains('hidden'),
                text: el.textContent,
                total: state.unreadTotal,
              };
            })()
            """
            % json.dumps(created["id"]),
            await_promise=True,
        )
        results.append((
            "⑧ 打开该会话后徽标消失 + 服务端未读清零",
            bool(cleared) and cleared.get("hidden") is True
            and cleared.get("total") == 0,
            f"{cleared}",
        ))

        # 服务端侧复核（不看前端自己报的 state，直接问后端）
        server_side = cdp.eval(
            """
            (async () => {
              const d = await (await fetch('/api/v1/unread')).json();
              return d;
            })()
            """,
            await_promise=True,
        )
        results.append((
            "⑧b 服务端 /unread 也确实清了（不是只改了前端变量）",
            bool(server_side) and server_side.get("total") == 0,
            f"{server_side}",
        ))

        # ---- ⑨ 零 JS 报错 ----
        time.sleep(0.5)
        results.append((
            "⑨ 全程零 JS 异常",
            len(cdp.exceptions) == 0,
            f"exceptions={cdp.exceptions[:3]}",
        ))

        # ---- 收尾：删掉自建会话 ----
        for sid in made:
            cdp.eval(
                "(async () => { await fetch('/api/v1/sessions/' + %s, {method:'DELETE'}); return true; })()"
                % json.dumps(sid),
                await_promise=True,
            )
        left = cdp.eval(
            """
            (async () => {
              const d = await (await fetch('/api/v1/sessions')).json();
              return (d.data || []).filter(s => (s.title || '').startsWith('chk-umo-')).length;
            })()
            """,
            await_promise=True,
        )
        results.append(("⑩ 自建会话已清理（没留脏数据）", left == 0, f"leftover={left}"))

        cdp.close()
    finally:
        proc.terminate()

    print()
    allok = True
    for name, ok, detail in results:
        print(f"  {'✅' if ok else '❌'} {name}\n       {detail}")
        allok = allok and ok
    print()
    if allok:
        print(f"✅ 通过：umo/未读面板判据 {len(results)} 项全部满足")
        return 0
    print(f"❌ 失败：{sum(1 for _, o, _ in results if not o)}/{len(results)} 项未满足")
    return 1


if __name__ == "__main__":
    sys.exit(main())
