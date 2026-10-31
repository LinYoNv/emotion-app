# -*- coding: utf-8 -*-
"""persona 族路由的**装配级**判据 —— 覆盖「路由整族没进注册表 / 进去了却查不到」。

## 为什么必须有这个文件（2026-10-30 第三轮续，情绪报障）

现象（用户视角）：「自我」页的「从当前人格整理档案」报错；第 8 步主动消息测试也失败。

我第一版按「persona 整族注册丢失」去查，**查错了方向** —— 真实的注册清单里
persona 族 8 条**都在**，生产日志 `logs/serve.out:5977-5984` 逐条可查：

    /astrbot_plugin_private_companion/host/targets/<tid>/roles/<rid>/persona
    .../persona/command
    .../persona/profile-draft
    .../persona/profile-migration-plan
    .../persona/profile-migration-apply
    .../persona/standardize
    .../persona/tryouts
    .../persona/style-rules

（「persona: 0 条」是**正则只匹配 `/astrbot_plugin_private_companion/persona*`** 造成的
统计假象：真实前缀是 `/host/targets/<tid>/roles/<rid>/persona`。同理
`GET .../persona` 返回 404 是**真的没这条路由**，不该有。）

真正的根因在**handler 内部**，生产上真发一次请求的原文是：

    GET .../host/targets/setup-core-3979182137/roles/83ab42e9.../persona
    200 {"status":"error","code":"host_conversation_missing",
         "reason":"selected AstrBot conversation is unavailable"}

⇒ 路由通了，是「按 cid 反查会话」这一步在宿主兼容层里**静默返回空**：
真 AstrBot 的 `select(ConversationV2.persona_id).where(conversation_id == X)`
是**全局按 cid**（它的 `conversations` 表是扁平的），而宿主端口是**按 umo**
组织的（`get_conversation(umo, cid)`）。见 `compat/core/db/adapter.py` 的
`_find_anywhere` / `_known_umos`。

## 这个文件守的三件事（缺一条就会再犯）

1. **装配级存在性**：真装配之后，注册表里**必须**有 `/host/targets/.../persona`
   这条确切路径 —— 只数总数（`>= 70`）证明不了"这一族没丢"，那正是上一轮的判据漏洞。
2. **路由真的能派发**：真 HTTP + 真 handler，返回**不是 404**（`host_conversation_missing`
   这类业务码是 200 信封，属于"路由通了"）。
3. **按 cid 反查真的能命中**：宿主适配器必须能从 cid 找到那条会话，
   而不是静默返回 `None`（→ 插件报 `host_conversation_missing`）。

⚠️ 反向可证伪：把 `_known_umos` 去掉（回到"无 finder/无 known_umos → 返回 None"），
   `test_adapter_resolves_conversation_by_cid` 与 `test_persona_route_not_conversation_missing`
   必须红。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]
HOST_DIR = APP_ROOT / "host"
PLUGIN_ROOT = APP_ROOT / "plugins"
PLUGIN_NAME = "astrbot_plugin_private_companion"

for _p in (str(APP_ROOT), str(HOST_DIR), str(HOST_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytestmark = pytest.mark.skipif(
    not (PLUGIN_ROOT / PLUGIN_NAME).is_dir(),
    reason=f"真插件不存在：{PLUGIN_ROOT / PLUGIN_NAME}",
)

#: 与生产完全一致的多目标形状（`data/plugins/config/..._config.json` 实测）
MULTI_HOST = {
    "enabled": True,
    "installation_id": "main",
    "targets": [{
        "target_id": "setup-core-3979182137",
        "platform_id": "onebot-napcat1",
        "account_id": "3979182137",
        "bot_id": "bot-3979182137",
        "participant_id": "3412642082",
    }],
    "personas": [{
        "bot_id": "bot-3979182137",
        "persona_id": "莉莉丝",
        "role_id": "role-3979182137",
    }],
    "group_targets": [],
    "capabilities": [
        "configuration.read", "conversation.reply", "memory.propose", "memory.read",
        "memory.reflect", "memory.subscribe", "text",
    ],
    "delivery_mode": "onebot_private",
    "group_delivery_mode": "onebot_group",
}

PERSONA_PREFIX = "/astrbot_plugin_private_companion/host/targets/setup-core-3979182137/roles/"
PERSONA_SUFFIXES = (
    "",
    "/command",
    "/profile-draft",
    "/profile-migration-plan",
    "/profile-migration-apply",
    "/standardize",
    "/tryouts",
    "/style-rules",
)

UMO = "onebot-napcat1:FriendMessage:3412642082"
CID = "7eced1a3-9c52-411c-a09c-4fecf38b5b47"


# ---------------------------------------------------------------------------
# ① 装配级：注册表里必须有 persona 族的确切路径
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def assembled(tmp_path_factory):
    """真装配一次（module 级复用 —— 加载真插件要几十秒）。"""
    import json
    import shutil

    from emotion_app.infra.config import Config
    from emotion_app.storage.factory import build_store
    from emotion_app.web.app import create_app
    from emotion_app.web.auth import AuthManager

    token = "persona-routes-token-0001"
    tmp = tmp_path_factory.mktemp("persona-routes")

    # 全局配置：把 multi_host 段按生产形状写进**临时副本**（不碰仓库 data/）
    global_cfg = tmp / "cmd_config.json"
    source_global = APP_ROOT / "data" / "cmd_config.json"
    if source_global.is_file():
        shutil.copy2(source_global, global_cfg)
    else:  # 没有基线时给一份最小可解析的（同样不碰仓库）
        global_cfg.write_text("{}", encoding="utf-8")

    pdata = tmp / "plugin_data" / PLUGIN_NAME
    (pdata / "companion-7" / "setup").mkdir(parents=True, exist_ok=True)
    (pdata / "companion-7" / "setup" / "binding.json").write_text(json.dumps({
        "account_id": "3979182137", "confirmed": True, "participant_id": "3412642082",
        "persona_id": "莉莉丝", "platform_id": "onebot-napcat1", "schema_version": 2,
    }, ensure_ascii=False), encoding="utf-8")
    plugin_cfg_dir = tmp / "plugin_data" / "config"
    plugin_cfg_dir.mkdir(parents=True, exist_ok=True)
    (plugin_cfg_dir / f"{PLUGIN_NAME}_config.json").write_text(
        json.dumps({"multi_host": MULTI_HOST, "staging": {"profile_id": "main"}},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    cfg = Config()
    cfg.auth.access_token = token
    cfg.storage.sqlite_path = str(tmp / "t.db")
    cfg.plugins.enabled = True
    cfg.plugins.mount_web_apis = True
    cfg.plugins.plugin_dirs = str(PLUGIN_ROOT)
    cfg.plugins.data_dir = str(tmp / "plugin_data")
    cfg.plugins.source_mirror_dir = str(tmp / "plugin_src")
    cfg.plugins.global_config_path = str(global_cfg)
    cfg.plugins.auto_initialize = False
    cfg.validate()

    store = build_store(cfg.storage)

    async def boot():
        await store.open()
        app = create_app(cfg, store, AuthManager(access_token=token))
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp / "config_profile.json")
        service = app.config["EMOTION_PLUGIN_HOST"]
        report = await service.start()
        return app, service, report

    app, service, report = asyncio.run(boot())
    assert report is not None, "插件宿主没起来（看日志）"
    assert report.loaded >= 1, f"插件没加载成功：{report.failed}\n{report.summary()}"

    yield {"app": app, "service": service, "report": report, "token": token}

    async def shutdown():
        await service.stop()
        await store.close()

    asyncio.run(shutdown())


def _routes(report) -> list[tuple[str, list[str]]]:
    return [(a.route, list(a.methods)) for a in report.mounted]


def _persona_suffixes(routes) -> set[str]:
    """把 persona 族路由归一成「角色路径之后的后缀」（角色 id 由插件派生，不硬编码）。"""
    out: set[str] = set()
    for route in routes:
        if not route.startswith(PERSONA_PREFIX) or "/persona" not in route:
            continue
        tail = route[len(PERSONA_PREFIX):]           # <role_digest>/persona[/suffix]
        _role, _, rest = tail.partition("/")         # persona[/suffix]
        out.add(rest.split("persona", 1)[1] if "persona" in rest else "")
    return out


def test_persona_family_is_in_the_registry(assembled):
    """⭐ 判据核心：8 条 persona 路由**逐条**都在注册表里（不是数总数）。

    🔴 这一条守的是上一轮的判据漏洞：现有装配判据只断言
    `mounted_count >= 140` / 陪伴 `>= 70` —— **计数相等不等于组成正确**。
    整族丢掉 8 条时总数仍然远超下界，所以那时全绿（1749 项）却漏掉了这个缺口。
    """
    routes = {route for route, _ in _routes(assembled["report"])}
    persona_routes = sorted(r for r in routes
                            if r.startswith(PERSONA_PREFIX) and "/persona" in r)
    actual = _persona_suffixes(routes)
    expected = set(PERSONA_SUFFIXES)
    assert actual == expected, (
        "persona 族的路径组成不对（计数相等不算通过，必须逐条集合相等）。\n"
        f"缺失：{sorted(expected - actual)}\n"
        f"多余：{sorted(actual - expected)}\n"
        "已挂载的 persona 路由：\n"
        + ("\n".join(f"  {r}" for r in persona_routes) or "  （一条都没有）")
    )


def test_persona_get_route_is_mounted_with_GET(assembled):
    """`persona` 根路由必须是 **GET**（页面用 `apiGet` 打开草稿对话框）。"""
    routes = dict(_routes(assembled["report"]))
    roots = [route for route, methods in routes.items()
             if route.startswith(PERSONA_PREFIX) and route.endswith("/persona") and "GET" in methods]
    assert roots, (
        "没有找到 `.../persona` 的 GET 路由（页面 `role-profile-draft.js:142` 靠它取人格列表）。\n"
        "已挂载的 persona 路由：\n"
        + "\n".join(f"  {r} {m}" for r, m in routes.items() if "persona" in r)
    )


def test_persona_profile_draft_route_is_mounted_with_POST(assembled):
    """`profile-draft` 必须是 **POST**（页面 `role-profile-draft.js:123-127`）。"""
    routes = dict(_routes(assembled["report"]))
    found = [route for route, methods in routes.items()
             if route.startswith(PERSONA_PREFIX) and route.endswith("/persona/profile-draft")
             and "POST" in methods]
    assert found, (
        "没有找到 `.../persona/profile-draft` 的 POST 路由。\n"
        "已挂载的 persona 路由：\n"
        + "\n".join(f"  {r} {m}" for r, m in routes.items() if "persona" in r)
    )


# ---------------------------------------------------------------------------
# ② 真 HTTP：路由真的能派发（不是 404）
# ---------------------------------------------------------------------------


def test_persona_route_dispatches_not_404(assembled):
    """真 HTTP 打 persona 根路由 —— **不许 404**。

    ⚠️ 判据故意**不要求 200 + ok**：这份 fixture 没有真实 QQ 会话与模型，
    业务上必然返回 `staging_not_initialized` / `host_conversation_missing` 之类。
    这一条守的是"**路由存在且派发到了 handler**"（404 = 整族没挂上）。
    业务码是否正确由下一条单独守。
    """
    app = assembled["app"]
    token = assembled["token"]
    report = assembled["report"]
    route = next((r for r, m in _routes(report)
                  if r.startswith(PERSONA_PREFIX) and r.endswith("/persona") and "GET" in m), None)
    assert route is not None, "注册表里就没有 persona 根路由"

    async def run():
        client = app.test_client()
        resp = await client.get(f"/api/plugins/extensions/{route.lstrip('/')}",
                                headers={"Authorization": f"Bearer {token}"})
        body = await resp.get_data()
        return resp.status_code, body.decode("utf-8", "replace")

    status, text = asyncio.run(run())
    assert status != 404, (
        f"persona 路由派发成了 404（整族没挂上）：{route}\n响应：{text[:800]}"
    )
    assert "未找到该路由" not in text, f"宿主判定该路由未登记：{route}\n响应：{text[:800]}"


# ---------------------------------------------------------------------------
# ③ 根因判据：宿主适配器必须能从 cid 反查会话
# ---------------------------------------------------------------------------


class _Store:
    """最小 store 替身（真 `StoreConversationManager` 的数据形状）。"""

    def __init__(self, data):
        self.data = data

    async def load_sections(self, keys):
        return dict(self.data)

    async def update_sections(self, keys, change):
        raw = dict(self.data)
        result = change(raw)
        self.data = raw
        return result


def _manager(*, persona_id="莉莉丝"):
    from emotion_app.domain.conversation_manager import StoreConversationManager

    data = {"plugin_conversations": {
        CID: {"cid": CID, "platform_id": "onebot-napcat1", "user_id": UMO, "content": [],
              "title": None, "persona_id": persona_id, "token_usage": 0,
              "created_at": 1, "updated_at": 1,
              "history_revision": 0, "reset_revision": 0},
        UMO: {"sel": CID},
    }}
    return StoreConversationManager(_Store(data))


def test_adapter_resolves_conversation_by_cid():
    """⭐ 根因判据：`select(ConversationV2.persona_id).where(conversation_id==X)` 必须命中。

    🔴 反向可证伪：把 `_known_umos()` 换成 `return []`（= 修复前的行为），
       这条立刻红 —— 那时返回 `None`，插件判"会话不存在"，
       报的就是生产上那句 `host_conversation_missing`。
    """
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot_host.compat.core.db.adapter import HostDbAdapter
    from astrbot_host.compat.core.db.po import ConversationV2
    from sqlmodel import select

    adapter = HostDbAdapter(conversation_manager=_manager())

    async def run():
        async with adapter.get_db() as session:
            return (await session.execute(
                select(ConversationV2.persona_id).where(
                    ConversationV2.conversation_id == CID))).one_or_none()

    row = asyncio.run(run())
    assert row is not None, (
        "按 cid 反查返回了 None —— 插件会据此报 host_conversation_missing"
        "（真 AstrBot 的查询是全局按 cid，宿主端口按 umo，必须枚举 umo 反查）"
    )
    assert tuple(row) == ("莉莉丝",), f"解出的人设不对：{row!r}"


def test_adapter_returns_empty_persona_not_missing_conversation():
    """会话存在但未绑定人格时：返回 `(None,)`，**不是** `None`。

    区分这两者很重要：前者 = "会话在、只是没选人格"（页面显示为空态），
    后者 = "会话不存在"（插件报错）。修复前两者都被压成 `None`。
    """
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot_host.compat.core.db.adapter import HostDbAdapter
    from astrbot_host.compat.core.db.po import ConversationV2
    from sqlmodel import select

    adapter = HostDbAdapter(conversation_manager=_manager(persona_id=None))

    async def run():
        async with adapter.get_db() as session:
            return (await session.execute(
                select(ConversationV2.persona_id).where(
                    ConversationV2.conversation_id == CID))).one_or_none()

    row = asyncio.run(run())
    assert row is not None, "会话存在但被报成'不存在' —— 插件会报 host_conversation_missing"
    assert tuple(row) == (None,), f"未绑定人格应解出 (None,)，实得 {row!r}"


def test_known_umos_prefers_explicit_lister():
    """宿主显式提供 `known_umos()` 时优先用它（不依赖私有 `_load`）。"""
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot_host.compat.core.db.adapter import HostDbAdapter

    class Explicit:
        def __init__(self):
            self.called = 0

        async def known_umos(self):
            self.called += 1
            return [UMO]

        async def get_conversation(self, umo, cid):
            return {"user_id": umo, "cid": cid, "persona_id": "莉莉丝"}

        async def _load(self):  # 不该被走到
            raise AssertionError("有 known_umos 时不该回落到私有 _load()")

    manager = Explicit()
    adapter = HostDbAdapter(conversation_manager=manager)
    found = asyncio.run(adapter._known_umos())
    assert found == [UMO], found
    assert manager.called == 1


def test_known_umos_without_source_returns_empty_not_raises():
    """端口什么都不提供时返回空列表（如实为空，不抛、也不编造）。"""
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot_host.compat.core.db.adapter import HostDbAdapter

    class Bare:
        async def get_conversation(self, umo, cid):
            return None

    adapter = HostDbAdapter(conversation_manager=Bare())
    assert asyncio.run(adapter._known_umos()) == []


def test_context_db_is_ready_without_calling_get_db():
    """官方插件直接读 `context._db`（`native.py:76`），不走 `get_db()`。

    真实现里 `_db` 是**构造即就绪**的（`context.py:132`）。我们原来只在
    `get_db()` 里惰性补建 ⇒ `context._db` 恒为 None ⇒ 插件抛
    `host_conversation_unavailable`，页面「从当前人格整理档案」与第 8 步
    主动消息测试双双失败。
    """
    import inspect

    from astrbot_host.compat.core.star.context import Context

    assert isinstance(inspect.getattr_static(Context, "_db"), property), (
        "_db 必须是 property：官方插件直接读该属性，裸 None 字段会让它恒为空"
    )


def test_context_db_property_shares_instance_with_get_db():
    """`context._db` 与 `get_db()` 必须指向**同一个**适配器实例。"""

    class _Ports:
        conversation_manager = None

    from astrbot_host.compat.core.star.context import Context
    from astrbot_host.compat.core.db.adapter import HostDbAdapter

    ctx = Context.__new__(Context)
    ctx._ports = _Ports()
    ctx._db_instance = None
    direct = ctx._db
    assert isinstance(direct, HostDbAdapter), type(direct)
    assert ctx._db is direct, "两次读取必须返回同一实例"
