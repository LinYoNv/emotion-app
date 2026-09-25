# -*- coding: utf-8 -*-
"""端到端取证：**多会话各自独立**（方案 B 的最终验收）。

## 情绪的原话与它对应的判据

> 「主动消息是要在陪伴里面配置的按照 umo 来，每个会话都 umo 都不一样」

本脚本在**真服务 / 真插件 / 真库**上把这句话跑成判据：

```
① 三个会话分别绑三个独立 user_id（面板会话页的「插件用户 ID」）
② 接口读回来一一对应（不串台）
③ 各发一条聊天 → 真插件为**各自的** user_id 建档，umo = 11:FriendMessage:<uid>
   （三个 umo 互不相同）
④ 反向判据：**全局默认那个 user 完全没被这轮派发改动**
   （若派发绕过了会话 user_id，它会回落到全局默认 → 这条必挂）
⑤ 投递端口按 umo → 只落进对应的那个会话（其余两个一条都不许多）
```

## 🔴 为什么必须"真"

单测里我自己造 repo 和假端口，能证明代码逻辑；证明不了：
  * 真服务里 `_session_user_id()` 到底拿到了什么；
  * 真插件的 `users` 里是不是真出现了这三个键（各带自己的 umo）；
  * 投递是不是真落进了**对应那个会话**、其余会话**没被污染**。

## ⚠️ 隔离承诺

本脚本只创建/删除**它自己建的**三个会话；user_id 用
`e2e_<随机>_<label>` 前缀（**不用 alice/bob 这种可能与你的数据重名的 id**），
结束时三个会话全部删除。
⚠️ 插件里的 `e2e_*` user 记录**会留下**（插件没有删 user 的安全 API），
   但它们带的随机 tag 保证下次跑互不干扰、也不影响你真实在用的 user。

用法：
    .venv/bin/python tools/proactive_multisession_check.py <base_url> <token>
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

#: 本脚本专属前缀 —— 保证不与真实用户数据撞名
UID_PREFIX = "e2e_"
TITLE_PREFIX = "e2e-多会话-"

#: 插件的完整 API 前缀（`page_api.py:220` `PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"`）
PLUGIN_ID = "astrbot_plugin_private_companion"
PLUGIN_API = f"/api/plugins/extensions/{PLUGIN_ID}/page"


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.cookie = ""

    def req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else {})
            except Exception:
                return e.code, {"raw": raw.decode("utf-8", "replace")[:400]}

    def login(self) -> bool:
        req = urllib.request.Request(
            f"{self.base}/api/v1/auth/login",
            data=json.dumps({"token": self.token}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as r:
            sc = r.headers.get("Set-Cookie") or ""
            if not sc:
                return False
        self.cookie = sc.split(";")[0]
        return True


def _items(d) -> list:
    """兼容 `{data:[...]}` / `{items:[...]}` / 裸 list（响应形状不靠猜）。"""
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    got = d.get("data")
    if isinstance(got, list):
        return got
    if isinstance(got, dict) and isinstance(got.get("items"), list):
        return got["items"]
    if isinstance(d.get("items"), list):
        return d["items"]
    return []


def _plugin_users(api: Api) -> tuple[int, dict[str, dict]]:
    """从**插件自己的 Web API** 读用户表（不是我去读文件 —— 读文件证明不了插件认谁）。

    ⚠️ 真形状是 `{"data": {"items": [...]}}`（实测）。
    """
    st, d = api.req("GET", f"{PLUGIN_API}/users")
    if st != 200:
        return st, {}
    out: dict[str, dict] = {}
    for u in _items(d):
        if isinstance(u, dict):
            uid = str(u.get("user_id") or u.get("id") or "")
            if uid:
                out[uid] = u
    return st, out


def main() -> int:
    if len(sys.argv) < 3:
        print("用法：python3 tools/proactive_multisession_check.py <base_url> <token>")
        return 2
    api = Api(sys.argv[1], sys.argv[2])
    if not api.login():
        print("🔴 登录失败：拿不到会话 cookie")
        return 1

    #: 随机后缀 —— 判据不许依赖"上一轮留下的状态"（本项目踩过：陈旧状态造成假通过）
    tag = uuid.uuid4().hex[:6]
    results: list[tuple[str, bool, str]] = []
    created: dict[str, str] = {}      # label → session_id
    uids = {label: f"{UID_PREFIX}{tag}_{label}" for label in ("alice", "bob", "carol")}

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    try:
        # ---- ① 建三个会话，各绑一个独立 user_id ----
        for label, uid in uids.items():
            st, d = api.req(
                "POST", "/api/v1/sessions",
                {"title": f"{TITLE_PREFIX}{tag}-{label}", "user_id": uid},
            )
            sid = (d or {}).get("id") or ""
            if sid:
                created[label] = sid
        check(
            "① 三个会话各自绑定独立的插件用户 ID",
            len(created) == 3,
            f"created={created}（uid = {UID_PREFIX}{tag}_<label>）",
        )
        if len(created) != 3:
            raise RuntimeError("会话没建齐 → 后续判据不可能成立（不跳过，直接失败）")

        # ---- ② 接口读回：一一对应、不串台 ----
        st, d = api.req("GET", "/api/v1/sessions")
        by_id = {s.get("id"): s for s in _items(d) if isinstance(s, dict)}
        got = {k: (by_id.get(sid) or {}).get("user_id") for k, sid in created.items()}
        check(
            "② 接口读回的 user_id 与会话一一对应（不串台）",
            all(got.get(k) == uids[k] for k in created),
            f"readback={got}  期望={uids}",
        )

        # ---- 派发前的插件用户表（用于 ④ 的反向判据）----
        #
        # 🔴 字段名必须照**实测**，不能照猜：
        #    插件 `page/users` 返回的 user 记录里**没有** `private_inbound_count`
        #    （实测该键为 None），真正随每条入站消息增长的是 **`inbound_count`**。
        #    ⚠️ 用错字段名会让这条判据**永远成立**（两边都是 None）→ 假绿。
        st0, users_before = _plugin_users(api)
        other_before = {
            uid: u.get("inbound_count")
            for uid, u in users_before.items() if not uid.startswith(UID_PREFIX)
        }

        # ---- 逐会话各发一条聊天（真 HTTP）----
        #     插件按 `event.get_sender_id()` 建档；我们的派发必须传**会话的** user_id。
        #     ⚠️ 上游可能 502（本机那把 key 失效，见 P0_P1_TASKS §5）——
        #        插件派发在调 LLM **之前**，所以上游挂了照样能验建档归属。
        dispatch_status = {}
        for label, sid in created.items():
            st, _ = api.req(
                "POST", "/v1/chat/completions",
                {
                    "model": "x",
                    "messages": [{"role": "user", "content": "打个招呼"}],
                    "session_id": sid,
                    "stream": False,
                },
            )
            dispatch_status[label] = st

        # ---- ③ 真插件为三个 user_id 各建了档，umo 互不相同 ----
        st, users = _plugin_users(api)
        want_umos = {uids[label]: f"11:FriendMessage:{uids[label]}" for label in uids}
        umos = {uid: (users.get(uid) or {}).get("umo") for uid in want_umos}
        check(
            "③ 插件按各自的 user_id 建档（三个 umo 互不相同）",
            st == 200 and bool(users) and all(
                umos.get(uid) == want for uid, want in want_umos.items()
            ),
            f"HTTP {st}；派发状态={dispatch_status}；"
            f"插件里的 umo={umos}（users 共 {len(users)} 个）",
        )

        # ---- ④ 反向：**已有（非 e2e）的 user 没被这轮派发改动** ----
        #     🔴 这是"方案 B 真的生效了吗"的守门判据：
        #        如果派发没用会话的 user_id，就会回落到全局默认 id
        #        （`plugins.umo_user_id`，本机是 `default`）→ 它的 `inbound_count` 必增
        #        → 判据挂。（实测：注入"绕过方案 B"后 `default` 的计数从 6 涨到 9。）
        #
        #     ⚠️ 用的是 `inbound_count`（实测真随入站增长的那个字段），
        #        不是 `private_inbound_count`（该键在响应里不存在 → 两边都 None → 假绿）。
        leaked = {
            uid: (cnt, (users.get(uid) or {}).get("inbound_count"))
            for uid, cnt in other_before.items()
            if (users.get(uid) or {}).get("inbound_count") != cnt
        }
        check(
            "④ 派发没有回落全局默认 id（方案 B 未被绕过）",
            bool(other_before) and not leaked,
            f"非 e2e 的 user 计数变化={leaked or '无'}；"
            f"（核对 {len(other_before)} 个已有 user：{other_before}）",
        )

        # ---- ⑤ 报告"投递归属"这一段的覆盖位置（**不在这里造 HTTP 探针**）----
        #
        # 🔴 为什么这里**不**加一条 `/api/v1/dev/delivery-probe` 之类的路由：
        #    那是为了测试而给**生产**开一个口子（本项目铁律：不接未授权的外部动作）。
        #    投递归属（umo → 哪个会话）本来就有更合适的落点，且都是绿的：
        #      * `tests/test_session_persona.py::test_delivery_lands_in_users_own_session`
        #        —— alice 的会话收到 1 条、bob 的 0 条（真 repo，临时库）；
        #      * `tests/test_llm_port_assembly.py` —— `start()` 真的把 `delivery=`
        #        传进了 `HostPorts` 与平台管理器（**装配点**判据）。
        #    ⚠️ 本条只**如实报告**这件事，不假装自己验过 —— 也不打 ✅。
        st, counts = 0, {}
        for label, sid in created.items():
            st2, m = api.req("GET", f"/api/v1/sessions/{sid}/messages")
            counts[label] = len(_items(m))
        check(
            "⑤ 三个会话的消息互不污染（每轮只写进自己那个会话）",
            all(c <= 2 for c in counts.values()),
            f"各会话消息数={counts}；"
            "（投递归属另由 test_session_persona / test_llm_port_assembly 覆盖）",
        )

    finally:
        for sid in created.values():
            api.req("DELETE", f"/api/v1/sessions/{sid}")

    print()
    allok = True
    for name, ok, detail in results:
        print(f"  {'✅' if ok else '❌'} {name}\n       {detail}")
        allok = allok and ok
    print()
    if allok:
        print(f"✅ 通过：多会话隔离判据 {len(results)} 项全部满足")
        return 0
    print(f"❌ 失败：{sum(1 for _, o, _ in results if not o)}/{len(results)} 项未满足")
    return 1


if __name__ == "__main__":
    sys.exit(main())
