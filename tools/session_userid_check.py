# -*- coding: utf-8 -*-
"""真浏览器判据：会话页的「插件用户 ID」（方案 B）。

## 为什么必须是真浏览器

`curl` 只能证明**接口**有 `user_id` 字段，证明不了：
  * 面板上**有没有**这个输入框；
  * 选中会话时**会不会回填**它的值（回填是 JS 干的）；
  * 保存时**发不发**得出去。

⚠️ 本项目的老坑：`provider_select` 的 id 曾落错到外层容器
   （设到 `<span>` 而不是内层 `<select>`）→ 取值 `undefined`，
   而**接口测试全绿**。所以"有没有这个元素"和"值对不对"都要真浏览器验。

用法：python3 tools/session_userid_check.py <base_url> <token>
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
PORT = 9226


def _http_json(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read())


class CDP:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self._id = 0

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
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


def main() -> int:
    if len(sys.argv) < 3:
        print("用法：python3 tools/session_userid_check.py <base_url> <token>")
        return 2
    base, token = sys.argv[1].rstrip("/"), sys.argv[2]

    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
         "--no-sandbox", "--disable-gpu", "--remote-allow-origins=*", "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    results: list[tuple[str, bool, str]] = []
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

        # ---- 登录（用真表单，避免绕过前端逻辑）----
        cdp.send("Page.navigate", url=f"{base}/")
        time.sleep(2.5)
        cdp.eval(
            "document.getElementById('login-token').value=" + json.dumps(token) + ";"
            "document.getElementById('login-form').dispatchEvent("
            "new Event('submit',{cancelable:true}));"
        )
        time.sleep(2.5)

        # ---- ⓪ 自建夹具：**不依赖任何预存会话** ----
        #     🔴 原来它去列表里找标题含 'alice' 的会话 —— 那要求"库里恰好有"，
        #        换成别的机器/清理过数据就跑不起来（判据不该依赖现场状态）。
        #        现在自己建一个带随机 user_id 的会话，跑完自己删掉。
        fixture_title = f"chk-会话-{uuid.uuid4().hex[:8]}"
        fixture_uid = "chk" + uuid.uuid4().hex[:10]
        made = cdp.eval(
            """
            (async () => {
              const r = await fetch('/api/v1/sessions', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title: %s, user_id: %s}),
              });
              const d = await r.json();
              return {status: r.status, id: d.id || null};
            })()
            """
            % (json.dumps(fixture_title), json.dumps(fixture_uid)),
            await_promise=True,
        )
        results.append(
            ("⓪ 自建测试会话（判据不依赖预存数据）", bool(made) and bool(made.get("id")),
             f"title={fixture_title!r} user_id={fixture_uid!r} → {made}")
        )
        fixture_id = (made or {}).get("id")

        # ---- ① 输入框存在 ----
        exists = cdp.eval("!!document.getElementById('s-user-id')")
        results.append(("① 会话页有「插件用户 ID」输入框", bool(exists), f"found={exists}"))

        # ---- 去到会话页并选中刚才那个会话 ----
        # 🔴 每次跑用**全新随机值**（而不是固定的 'alice-renamed'）：
        #    上一轮注入验证实测到一个假通过 —— 固定值在库里**已经存在**
        #    （上次跑留下的），于是"保存真的写进去了吗"这条判据
        #    被**陈旧状态**满足：注入"白名单删掉 user_id"后它照样全绿。
        #    通则：判据不许用"上一轮自己造成的状态"当证据。
        fresh = "chk" + uuid.uuid4().hex[:10]
        nav = cdp.eval(
            """
            (async () => {
              if (location.hash !== '#/session') { location.hash = '#/session'; }
              await new Promise(r => setTimeout(r, 900));
              // 列表可能还没刷新出新会话 —— 走 **hash 重进**这条真 UI 路径
              // （`applyRoute` 的 session 分支会 `loadSessions()`；
              //  ⚠️ 面板没有单独的刷新按钮，实测 index.html:90 只有 <ul id="session-list">）
              const other = location.hash === '#/session' ? '#/persona' : '#/session';
              location.hash = other;
              await new Promise(r => setTimeout(r, 500));
              location.hash = '#/session';
              await new Promise(r => setTimeout(r, 1100));
              const items = [...document.querySelectorAll('#session-list li')];
              const el = items.find(x => x.textContent.includes(%s));
              if (!el) return {err: '列表里找不到刚建的会话',
                               all: items.map(x=>x.textContent.trim())};
              el.click();
              await new Promise(r => setTimeout(r, 1100));
              const inp = document.getElementById('s-user-id');
              return {value: inp ? inp.value : null,
                      visible: inp ? !inp.closest('form').classList.contains('hidden') : false};
            })()
            """
            % json.dumps(fixture_title), await_promise=True,
        )
        ok2 = bool(nav) and nav.get("value") == fixture_uid
        results.append(
            ("② 选中会话后回填它绑的 user_id", ok2,
             f"value={nav.get('value')!r}（期望 {fixture_uid!r}）nav={nav}")
        )

        # ---- ③ 改一个值并保存，然后从接口读回 ----
        save = cdp.eval(
            """
            (async () => {
              const inp = document.getElementById('s-user-id');
              if (!inp) return {err: 'no input'};
              inp.value = %s;
              document.getElementById('session-form').dispatchEvent(
                new Event('submit', {cancelable: true}));
              await new Promise(r => setTimeout(r, 1200));
              return {hint: (document.getElementById('session-hint')||{}).textContent || ''};
            })()
            """
            % json.dumps(fresh), await_promise=True,
        )
        # 用接口复读（**不信前端提示**：提示可能是"已保存"而请求失败）
        readback = cdp.eval(
            """
            (async () => {
              const d = await (await fetch('/api/v1/sessions')).json();
              const items = d.data || d.items || [];
              const a = items.find(s => s.user_id === %s);
              return {found: !!a, title: a ? a.title : null,
                      anyOld: items.some(s => s.user_id === 'alice')};
            })()
            """
            % json.dumps(fresh), await_promise=True,
        )
        ok3 = bool(readback) and readback.get("found") is True
        results.append(
            ("③ 保存后接口里真的改了", ok3, f"readback={readback} hint={save}")
        )

        # ---- ④ 非法值（带冒号）必须被拒，且**不写进去** ----
        bad = cdp.eval(
            """
            (async () => {
              const inp = document.getElementById('s-user-id');
              inp.value = 'bad:colon';
              document.getElementById('session-form').dispatchEvent(
                new Event('submit', {cancelable: true}));
              await new Promise(r => setTimeout(r, 1200));
              const hint = (document.getElementById('session-hint')||{}).textContent || '';
              const d = await (await fetch('/api/v1/sessions')).json();
              const items = d.data || d.items || [];
              return {hint, stillOld: items.some(s => s.user_id === %s),
                      leaked: items.some(s => s.user_id === 'bad:colon')};
            })()
            """
            % json.dumps(fresh), await_promise=True,
        )
        ok4 = (bad or {}).get("leaked") is False and (bad or {}).get("stillOld") is True
        results.append(
            ("④ 带冒号的非法值被拒且未写入", ok4, f"{bad}")
        )

        # ---- ⑤ 收尾：删掉自建会话（**不留脏数据**）----
        cleanup = cdp.eval(
            """
            (async () => {
              const r = await fetch('/api/v1/sessions/' + %s, {method: 'DELETE'});
              const d = await (await fetch('/api/v1/sessions')).json();
              const items = d.data || d.items || [];
              return {deleteStatus: r.status,
                      gone: !items.some(s => s.id === %s),
                      leftover: items.some(s => s.user_id === %s)};
            })()
            """
            % (json.dumps(fixture_id), json.dumps(fixture_id), json.dumps(fresh)),
            await_promise=True,
        )
        ok5 = bool(cleanup) and cleanup.get("deleteStatus") == 200 \
            and cleanup.get("gone") is True and cleanup.get("leftover") is False
        results.append(("⑤ 自建测试会话已删除，没留脏数据", ok5, f"{cleanup}"))

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
        print(f"✅ 通过：会话 user_id 判据 {len(results)} 项全部满足")
        return 0
    print(f"❌ 失败：{sum(1 for _,o,_ in results if not o)}/{len(results)} 项未满足")
    return 1


if __name__ == "__main__":
    sys.exit(main())
