# -*- coding: utf-8 -*-
"""集成判据：**插件回复真的能出现在聊天响应里，且不吞掉正常聊天**。

这是 P0 的最终验收 —— 前面所有兼容层/派发器工作，最终要在
"用户发一句话 → 聊天接口返回什么"这条真实路径上体现出来。

🔴 本文件守的两个真实风险：
  1. **插件吞掉聊天**：普通输入（"你好呀"）被判成"插件回复" →
     再也不走 LLM → 聊天功能直接废掉。**这是最严重的一类回归。**
  2. **重复回复**：接管型指令（如 `陪伴`）产出为空但 `stopped=True`，
     只看 `emitted` 会判成"没说话" → 又调 LLM → 用户收到两条矛盾回复。

⚠️ 关键：下面测的是**生产代码里那一份规则**
（`emotion_app.web.plugin_host.plugin_reply_text`），
**不是测试里复刻的副本** —— 副本会导致自证式假通过。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))


class _Res:
    """模拟 DispatchResult（只带集成层关心的字段）。"""

    def __init__(self, emitted=None, stopped=False, executed=None, errors=None):
        self.emitted = emitted or []
        self.stopped = stopped
        self.executed = executed or []
        self.errors = errors or []


class _Host:
    """假插件宿主：只实现 dispatch_message。"""

    def __init__(self, res=None, raises=None):
        self.res = res
        self.raises = raises
        self.calls: list[str] = []

    async def dispatch_message(self, text, **kw):
        self.calls.append(text)
        if self.raises is not None:
            raise self.raises
        return self.res


# ---------------------------------------------------------------------------
# 一、判定规则（测生产代码那一份）
# ---------------------------------------------------------------------------
def test_plain_chat_falls_through_to_llm():
    """🔴 **最严重回归的判据**：普通聊天必须返回 None（= 走 LLM）。

    普通输入会命中若干常驻 handler，但它们不产出内容
    （`emitted=[]`、`stopped=False`）→ 必须判成"插件没说话"。
    """
    from emotion_app.web.plugin_host import plugin_reply_text

    res = _Res(emitted=[], stopped=False, executed=["on_message", "auto_parse"])
    assert plugin_reply_text(res) is None, (
        "普通聊天被判成'插件回复' → 聊天不再走 LLM，功能直接消失"
    )

    # 只有空白的产出同样不算说话
    assert plugin_reply_text(_Res(emitted=["", "  "], stopped=False)) is None


def test_takeover_command_counts_as_reply():
    """🔴 接管型指令：产出为空但 `stopped=True` → **算插件回复**。

    实测 `陪伴` 就是这种：走流式异步回、返回空串，但 `stop_event()`。
    只看 `emitted` 会导致重复调 LLM。
    """
    from emotion_app.web.plugin_host import PLUGIN_TAKEOVER_PLACEHOLDER, plugin_reply_text

    res = _Res(emitted=["", ""], stopped=True)
    text = plugin_reply_text(res)
    assert text is not None, (
        "stopped=True 却判成'没说话' → 会重复调 LLM，用户收到两条矛盾回复"
    )
    assert text == PLUGIN_TAKEOVER_PLACEHOLDER, (
        "接管但无同步产出时应给**如实**占位，不许编造内容"
    )


def test_real_output_wins_over_placeholder():
    """有真实产出时用产出文本（多段用换行拼接）。"""
    from emotion_app.web.plugin_host import plugin_reply_text

    res = _Res(emitted=["正在检查生图服务...", "上游 token=OK"], stopped=False)
    assert plugin_reply_text(res) == "正在检查生图服务...\n上游 token=OK"


def test_result_none_or_host_absent():
    """宿主不可用（返回 None）→ 判定为"没说话"，聊天照常。"""
    from emotion_app.web.plugin_host import plugin_reply_text

    assert plugin_reply_text(None) is None


def test_dispatch_exception_does_not_break_chat():
    """插件派发抛异常 → 集成层吞掉并返回 None（聊天不受影响）。

    ⚠️ 吞异常是**有意**的（插件是可选增强），但生产代码里必须打日志
       —— 这里只验"不阻断"，日志由 `dispatch_message` 保证。
    """
    host = _Host(raises=RuntimeError("插件炸了"))

    async def call():
        try:
            await host.dispatch_message("你好")
        except Exception:
            return None
        return "REPLY"

    assert asyncio.run(call()) is None


# ---------------------------------------------------------------------------
# 二、宿主服务的接线（真加载插件 + 真派发）
# ---------------------------------------------------------------------------
def test_host_service_dispatch_real_plugin(tmp_path, monkeypatch):
    """真插件经 `PluginHostService.dispatch_message` 能拿到真实回复。

    ⚠️ 这条**真的加载插件**（走镜像），比上面慢，但它是
       "端到端真的通了"的证据 —— 没有它，上面全是逻辑自证。
    """
    plugin_root = APP_DIR / "plugins"
    if not plugin_root.is_dir():
        pytest.fail(
            f"插件目录不存在：{plugin_root}（这不是 skip —— 前置条件不满足即判据失败）"
        )

    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.loader import PluginLoader
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()

    class _Cfg:
        class plugins:  # noqa: N801
            umo_user_id = "3412642082"

    async def run():
        # 🔴 **必须隔离数据目录**（2026-10-19 修）
        #
        # 原来没调 `set_data_root()` ⇒ 用**全局默认** `data/plugins/`。
        # 同一个 pytest 进程里别的测试也往那里写过 ⇒ 全量跑时
        # `memory_companion` 读到的是一个**被别的测试中途留下**的
        # 空/半初始化库，`core/store.py` 的 `_stats_sync` 查 `memories` 表
        # 直接 `sqlite3.OperationalError: no such table: memories`。
        # 实测：单跑本文件绿、全量跑红 —— 正是"判据依赖跨测试漂移的进程级状态"。
        set_data_root(str(tmp_path / "plugin_data"))
        loader = PluginLoader(
            plugin_dirs=[str(plugin_root)], mirror_root=str(tmp_path / "mirror")
        )
        report = await loader.load_all(ports=HostPorts(config={}))
        # 🔴 **必须跑 `initialize_all()`**（2026-10-19 补，真缺陷）
        #
        # 记忆插件在**全新的**数据目录里，`memory_companion.db` 是**空文件**，
        # 建表发生在 `initialize()` 里（实测：只 `load_all` → 0 字节；
        # 加了 `initialize_all()` → 745KB、`memories` 等表齐全）。
        # 本判据原来没调它 ⇒ 数据目录一旦是干净的，`cmd_mcomp_status`
        # 立刻报 `no such table: memories`。
        # ⚠️ 以前它是绿的，因为默认数据目录 `data/plugins/` 里有**历史库**把
        #    这个缺陷盖住了 —— 典型的"判据依赖环境里已有的状态"。
        init_failed = await loader.initialize_all(report)
        assert init_failed == [], f"插件初始化失败：{init_failed}"

        from astrbot_host.dispatch import dispatch
        from astrbot_host.umo import build_inbound_event

        ev = build_inbound_event("mcomp status", "3412642082")
        # umo 必须与生产数据逐字同构（这是 P1-1 拍板的约定）
        assert ev.unified_msg_origin == "11:FriendMessage:3412642082", (
            f"umo 与生产不同构：{ev.unified_msg_origin!r}"
        )
        return await dispatch(ev)

    res = asyncio.run(run())
    joined = "\n".join(str(x) for x in res.emitted)
    assert res.emitted, f"真插件应产出回复，实际 emitted={res.emitted} errors={res.errors}"
    assert "记忆" in joined or "记住" in joined, (
        f"回复内容不像 memory_companion 的输出：{joined[:200]!r}"
    )


def test_umo_user_id_is_configurable():
    """umo 的 `session_id` 段必须**可配置**（它是插件存数据的主键）。

    ⚠️ 一改就等于换库 —— 所以它不能是硬编码，也不该随机器变化。
    """
    from emotion_app.infra.config import PluginsConfig

    cfg = PluginsConfig()
    assert cfg.umo_user_id == "default"
    cfg2 = PluginsConfig(umo_user_id="3412642082")
    assert cfg2.umo_user_id == "3412642082"


# ---------------------------------------------------------------------------
# 三、P1-4：绑定链路（面板「已检测到 N 个私聊绑定」的数据来源）
# ---------------------------------------------------------------------------
def test_plain_message_does_not_grant_delivery(tmp_path):
    """🔴 **收到普通消息 ≠ 同意接收主动消息**（未经同意的行为变更）。

    ## 这条判据的前身与它为什么要重写（2026-10-19）

    前身 `test_binding_command_is_the_only_path_that_binds` 验证的是 6.7.0 的
    两个内部字段：

      * `user["umo"]`                —— 普通消息触发，只"观察"到
      * `user["bound_delivery_umo"]` —— `陪伴 绑定主动消息` 指令触发，才算"绑定"

    **7.0.0 把这两样都删了**，实测确认：

      * 实例上**没有 `data` 属性**（`dir()` 零命中）；
      * 全仓库 grep `绑定主动消息` → **零命中**（那条指令不存在了）；
      * 投递授权改由 V2 的 `identity_policy`（`delivery_grant`，
        `companion_v2/identity_policy/grants.py`）承载，且需要
        **在面板里配置 Bot 账号 + 人格**后才可能建立（未配置时
        `host_mappings_required`，`astrbot_host/multi_settings.py:167`）。

    ## ⚠️ 但它守的那条安全属性**一点都不过时**

    "主动消息会**主动打扰用户**，所以必须是用户显式同意，绝不能
    '收到过消息就自动算同意'"。7.0.0 的等价观测点是：
    **全新数据目录 + 只发普通消息 → 不该产生任何投递授权**。

    判据因此改成（不依赖任何插件私有结构）：
      ① 派发一条普通消息，插件入站入口**确实收到**（对照组，证明链路通）；
      ② 临时数据目录里**没有**任何 `companion-v2.sqlite3` 被写出
         —— 7.0.0 在未注册账号/人格时**完全不落盘**，
         这本身就是"没有建立任何授权"的机器可验证据；
      ③ 若将来它落了盘，再按 `delivery_grant` 表查（这里显式留出上升路径）。
    """
    import logging
    import tempfile

    logging.disable(logging.ERROR)
    plugin_root = APP_DIR / "plugins"
    if not plugin_root.is_dir():
        pytest.fail(f"插件目录不存在：{plugin_root}（前置条件不满足=判据失败）")

    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.dispatch import dispatch
    from astrbot_host.loader import PluginLoader
    from astrbot_host.platform_manager import build_platform_manager
    from astrbot_host.registry import star_handlers_registry
    from astrbot_host.shadow import install_shadow_package
    from astrbot_host.umo import build_inbound_event

    install_shadow_package()

    data_root = tempfile.mkdtemp()

    async def run():
        set_data_root(data_root)                   # 隔离：全新数据目录
        loader = PluginLoader(
            plugin_dirs=[str(plugin_root)], mirror_root=str(tmp_path / "mirror")
        )
        rep = await loader.load_all(
            ports=HostPorts(config={}, platform_manager=build_platform_manager())
        )
        rec = next(
            r for r in rep.records if r.name == "astrbot_plugin_private_companion"
        )
        assert rec.ok, f"插件加载失败：{rec.error}"

        # 装计数器（包注册表里的 partial，见 `_real_plugin_stream_probe` 的说明）
        hits: list[str] = []
        for h in star_handlers_registry:
            if getattr(h, "handler_name", "") != "receive_companion_message":
                continue
            original = h.handler

            async def _counting(event, *a, __orig=original, **kw):
                try:
                    hits.append(str(getattr(event, "unified_msg_origin", "") or ""))
                except Exception:  # noqa: BLE001
                    hits.append("")
                return await __orig(event, *a, **kw)

            h.handler = _counting
            break

        await dispatch(build_inbound_event("你好呀", "3412642082"))
        return hits

    hits = asyncio.run(run())

    # ① 对照组：插件确实收到了这条普通消息（否则"没有授权"只是整条链没通）
    assert hits, (
        "普通消息根本没送到插件入站入口 —— 那这条判据证明不了任何事"
    )

    # ② 7.0.0 在未注册账号/人格时**完全不落盘** ⇒ 没有任何持久化授权被写出
    import pathlib as _pathlib

    db_files = [
        p for p in _pathlib.Path(data_root).rglob("companion-v2*.sqlite3")
    ]
    assert not db_files, (
        "🔴 只发了一条普通消息就产生了 V2 数据库（= 建立了持久化状态）："
        f"{db_files}。若这是 7.0.0 的新行为，判据必须改成直接查 `delivery_grant` 表"
        "（`companion_v2/identity_policy/grants.py`），而不是简单放行。"
    )


# ---------------------------------------------------------------------------
# 四、P0-4：LLM 钩子（提示词注入）
# ---------------------------------------------------------------------------
def _msgs():
    from emotion_app.domain.message import Message

    return [
        Message(role="system", content="你是芙芙。"),
        Message(role="user", content="你好呀"),
    ]


def test_messages_roundtrip_preserves_content():
    """`list[Message]` → `ProviderRequest` → 转回：**内容不许丢**。

    🔴 这是注入链路的地基：如果转换时丢了 system_prompt 或上下文，
       插件就算注入成功也会被转回时抹掉（表现=注入静默失效）。
    """
    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.compat.core.provider.entities import ProviderRequest

    from emotion_app.web.plugin_host import (
        _messages_to_provider_request,
        _provider_request_to_messages,
    )

    msgs = _msgs()
    req, ctx = _messages_to_provider_request(msgs, ProviderRequest)

    assert req.system_prompt == "你是芙芙。", f"system 没进去：{req.system_prompt!r}"
    assert req.prompt == "你好呀", f"最后一条 user 没取到：{req.prompt!r}"
    assert ctx == [{"role": "user", "content": "你好呀"}]

    back = _provider_request_to_messages(req, ctx)
    assert [(m.role, m.content) for m in back] == [
        ("system", "你是芙芙。"),
        ("user", "你好呀"),
    ], f"往返后内容变了：{[(m.role, m.content) for m in back]}"


def test_hook_injection_flows_back_into_messages():
    """🔴 **核心判据**：钩子改 `req.system_prompt` 后，转回的 messages 里必须有它。

    这正是"记忆注入"在实现层面的全部含义。
    """
    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.compat.core.provider.entities import ProviderRequest

    from emotion_app.web.plugin_host import (
        _messages_to_provider_request,
        _provider_request_to_messages,
    )

    msgs = _msgs()
    req, ctx = _messages_to_provider_request(msgs, ProviderRequest)

    # 模拟插件注入（真插件就是这么改的）
    req.system_prompt += "\n【记忆】你上次说喜欢猫。"

    back = _provider_request_to_messages(req, ctx)
    assert back[0].role == "system"
    assert "【记忆】你上次说喜欢猫。" in back[0].content, (
        f"注入没回到 messages：{back[0].content!r} —— 这就是记忆注入静默丢失的形态"
    )


def test_hook_extra_user_content_parts_becomes_visible_message():
    """插件用 `extra_user_content_parts` 注入时，也要落到 messages 里。"""
    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.compat.core.provider.entities import ProviderRequest

    from emotion_app.web.plugin_host import (
        _messages_to_provider_request,
        _provider_request_to_messages,
    )

    req, ctx = _messages_to_provider_request(_msgs(), ProviderRequest)
    req.extra_user_content_parts.append({"type": "text", "text": "【提醒】该喝水了"})

    back = _provider_request_to_messages(req, ctx)
    joined = "\n".join(m.content for m in back)
    assert "【提醒】该喝水了" in joined, f"内容块丢了：{joined!r}"


def test_chat_service_calls_hook_both_paths():
    """`ChatService` 的**非流式与流式都要**调钩子。

    ⚠️ 只接一条路的后果很隐蔽：流式聊天没有记忆、非流式有
       （或反过来）—— 用户会觉得"有时候记得、有时候不记得"。
    """
    import inspect

    from emotion_app.domain import chat as chat_mod

    src = inspect.getsource(chat_mod.ChatService)
    assert src.count("_apply_llm_hook(") >= 3, (
        "钩子调用点应 >= 3 处（1 个定义 + complete + stream），"
        f"实际 {src.count('_apply_llm_hook(')} 处 —— 可能漏接了某条路径"
    )


def test_hook_port_absent_means_unchanged():
    """未注入端口 → 钩子不跑，消息**原样返回**（默认关闭，聊天行为不变）。"""
    from emotion_app.domain.chat import ChatService

    svc = ChatService.__new__(ChatService)     # 不跑 __init__（避免依赖 config）
    svc._llm_hook = None

    msgs = _msgs()
    out = asyncio.run(svc._apply_llm_hook(msgs))
    assert out is msgs, "未注入端口时不该改动消息"


def test_hook_failure_does_not_break_chat():
    """钩子抛错 → **原样返回**（记 ERROR），聊天不受影响。"""
    from emotion_app.domain.chat import ChatService

    svc = ChatService.__new__(ChatService)

    async def broken(messages):
        raise RuntimeError("插件钩子炸了")

    svc._llm_hook = broken
    msgs = _msgs()
    out = asyncio.run(svc._apply_llm_hook(msgs))
    assert out is msgs, "钩子抛错时不该影响聊天（应原样返回）"


def test_hook_returning_garbage_is_ignored():
    """钩子返回非列表 → 原样返回（不许把消息列表换成别的东西）。"""
    from emotion_app.domain.chat import ChatService

    svc = ChatService.__new__(ChatService)

    async def bad(messages):
        return "我不是列表"

    svc._llm_hook = bad
    msgs = _msgs()
    assert asyncio.run(svc._apply_llm_hook(msgs)) is msgs


def test_real_plugin_hooks_inject_into_messages(tmp_path):
    """🔴 **端到端**：真插件的钩子经适配层，真的把内容注入进了我们的 messages。

    这是 P0-4 的最终验收 —— 前面所有转换/接线都对，最终要在这里体现。
    """
    import logging

    logging.disable(logging.ERROR)
    plugin_root = APP_DIR / "plugins"
    if not plugin_root.is_dir():
        pytest.fail(f"插件目录不存在：{plugin_root}（前置条件不满足=判据失败）")

    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.platform_manager import build_platform_manager
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()

    class _Plugins:
        umo_user_id = "3412642082"

    class _Cfg:
        plugins = _Plugins

    async def run():
        from emotion_app.web.plugin_host import PluginHostService

        svc = PluginHostService(_Cfg())
        loader = PluginLoader(
            plugin_dirs=[str(plugin_root)], mirror_root=str(tmp_path / "mirror")
        )
        await loader.load_all(
            ports=HostPorts(config={}, platform_manager=build_platform_manager())
        )
        # 直接给 host 挂上 loader 的注册表（与生产 PluginHost 等价）
        svc.host = type("H", (), {})()          # 非 None 即可（apply_llm_hooks 只判空）
        return await svc.apply_llm_hooks(_msgs())

    out = asyncio.run(run())
    assert out is not None, "钩子应返回消息列表（None 表示注入被跳过）"
    joined = "\n".join(m.content or "" for m in out)
    assert len(joined) > 200, (
        f"真插件钩子似乎没注入任何内容（总长 {len(joined)}）：{joined[:200]!r}"
    )
    # 至少一条 system 消息承载注入
    assert any(m.role == "system" and len(m.content or "") > 100 for m in out), (
        "注入的内容应出现在 system 消息里"
    )


def test_injected_content_reaches_upstream_payload(tmp_path):
    """🔴🔴 **最硬的一条**：注入的内容必须出现在**发给上游 HTTP 请求的 body 里**。

    为什么必须有这条：上面几条只验到"messages 里有了注入"，
       但**从 messages 到 HTTP body** 还有一段（`to_upstream()` 的字段映射）。
       本项目踩过同类坑：`to_upstream()` 漏传字段 → 上游 400，
       **单测全绿、只有打真实上游才现形**。
       → 所以这里用 `httpx.MockTransport` 截住**真正发出去的 body** 来验。

    判据：body 的 system 消息里含插件注入的标志串（如 `<MemoryCompanion`）。
    """
    import json as _json
    import logging

    logging.disable(logging.ERROR)
    plugin_root = APP_DIR / "plugins"
    if not plugin_root.is_dir():
        pytest.fail(f"插件目录不存在：{plugin_root}（前置条件不满足=判据失败）")

    sys.path.insert(0, str(APP_DIR / "host"))
    import httpx

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.platform_manager import build_platform_manager
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "好呀"}, "finish_reason": "stop"}
                ],
                "usage": {},
                "model": "stub",
            },
        )

    class _Plugins:
        umo_user_id = "3412642082"

    class _Cfg:
        plugins = _Plugins

    async def run():
        from emotion_app.domain.backends import OpenAIChatBackend
        from emotion_app.domain.chat import ChatRequest, ChatService
        from emotion_app.web.plugin_host import PluginHostService

        # 1) 真插件宿主（拿钩子）
        svc = PluginHostService(_Cfg())
        loader = PluginLoader(
            plugin_dirs=[str(plugin_root)], mirror_root=str(tmp_path / "mirror")
        )
        await loader.load_all(
            ports=HostPorts(config={}, platform_manager=build_platform_manager())
        )
        svc.host = object()

        # 2) 打桩上游，截住真正发出去的 body
        backend = OpenAIChatBackend(transport=httpx.MockTransport(handler))
        await backend.open()      # ⚠️ 不 open 就调 → RuntimeError（我第一版漏了）

        # 3) 用**真的 UpstreamConfig dataclass**（不是手搓桩对象）——
        #    ⚠️ 我第一版手搓了个只有 3 个字段的类，结果 `resolve_target` 里
        #       读 `timeout_seconds` 直接 AttributeError。
        #       教训:**桩要长得像真的**，否则测的是"我的桩"而不是产品代码。
        from emotion_app.infra.config import Config, UpstreamConfig

        cfg = Config()
        cfg.upstream = UpstreamConfig(
            base_url="https://stub.invalid/v1", api_key="k", model="stub"
        )

        service = ChatService(cfg, client=backend)
        service.bind_llm_hook(svc.apply_llm_hooks)

        req = ChatRequest(messages=[], stream=False, persist=False)
        from emotion_app.domain.message import Message

        req.messages = [Message(role="user", content="我今天有点累")]
        await service.complete(req)
        return captured.get("body")

    body = asyncio.run(run())
    assert body, "没有截到上游请求体"
    messages = body.get("messages") or []
    joined = "\n".join(
        str(m.get("content", "")) for m in messages if isinstance(m, dict)
    )
    assert len(joined) > 300, (
        f"发出去的 body 里没有插件注入的内容（总长 {len(joined)}）—— "
        "说明注入在 messages→payload 之间丢了"
    )
    assert "MemoryCompanion" in joined or "记忆" in joined, (
        f"没看到记忆插件的注入标志：{joined[:300]!r}"
    )


# ---------------------------------------------------------------------------
# 五、P0-2 真机验收：**真插件**经**真流式 HTTP 路由**被派发到
# ---------------------------------------------------------------------------
#
# 🔴 为什么必须有这一条（本项目最贵的一课）：
#    「单测全绿 + 接口判据全绿 + 页面能打开 —— 全都不算证据」。
#    上面几条用的是**假宿主**，它们能证明"接线对了"，但证明不了
#    "真插件在真路由上真的被调用了、真的建了档"。
#    2026-09-26 那个真 bug（流式分支完全没有派发）**在假宿主判据下永远绿**
#    —— 因为假宿主不关心"插件有没有收到"，只关心"我有没有被调用"。
#
# ⚠️ 判据落在**插件的真实数据**上（`data["users"]` 里的 umo），不是日志、
#    不是返回值 —— 这是"插件真的把它当成一次入站消息"的唯一证据。
def _real_plugin_stream_probe(tmp_path):
    """🔴 真插件 + 真 `/v1/chat/completions` + `stream: true` → **插件真的建档**。

    ⚠️ 上游用假 backend：派发发生在"调上游之前"，
       所以上游能不能通**不影响**这条判据（这也是当初差分实验成立的前提）。

    ⚠️ 两条路（`stream: true` / `false`）**用不同的 user_id** 各跑一次 ——
       同一 app、同一插件、唯一变量 = `stream` 开关 + 会话绑的 umo。
       这样"流式那条没建档"与"非流式建档了"是两个**互不相同**的 umo，
       不会互相掩盖。
    """
    import logging

    logging.disable(logging.ERROR)
    plugin_root = APP_DIR / "plugins"
    if not plugin_root.is_dir():
        pytest.fail(f"插件目录不存在：{plugin_root}（这不是 skip，前置不满足=判据失败）")

    sys.path.insert(0, str(APP_DIR / "host"))
    import tempfile

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.dispatch import dispatch as real_dispatch
    from astrbot_host.loader import PluginLoader
    from astrbot_host.platform_manager import build_platform_manager
    from astrbot_host.shadow import install_shadow_package
    from astrbot_host.umo import build_inbound_event

    install_shadow_package()
    #: 与生产**同一个**令牌字面量（`tests/conftest.py` 的 TOKEN）
    token = "t" * 20

    async def run():
        from emotion_app.domain.chat import ChatService
        from emotion_app.domain.session_repo import SessionRepository
        from emotion_app.infra.config import Config
        from emotion_app.storage.sqlite_store import SqliteStore
        from emotion_app.web.app import create_app
        from emotion_app.web.auth import AuthManager
        from emotion_app.web.plugin_host import PLUGIN_TAKEOVER_PLACEHOLDER

        set_data_root(tempfile.mkdtemp())

        # 1) 真插件宿主服务（复用生产那条装配路径的等价物）
        from emotion_app.web.plugin_host import PluginHostService

        class _Plugins:
            umo_user_id = "default"
            data_dir = str(tmp_path / "plugin_data")
            global_config_path = ""
            plugin_dirs = str(plugin_root)
            source_mirror_dir = str(tmp_path / "mirror")
            mirror_sources = True
            auto_initialize = False
            mount_web_apis = True
            enabled = True
            default_platform_id = ""
            extra_platforms = "[]"
            initialize_timeout = 20.0

        class _Cfg:
            plugins = _Plugins

        svc = PluginHostService(_Cfg())
        loader = PluginLoader(
            plugin_dirs=[str(plugin_root)], mirror_root=str(tmp_path / "mirror")
        )
        # ⚠️ `load_all()` **返回** LoadReport（不是挂在 loader 上）——
        #    与「装配判据」同款：拿到真实例才能断言"插件真的把这条消息当入站"。
        report = await loader.load_all(
            ports=HostPorts(config={}, platform_manager=build_platform_manager())
        )
        inst = next(
            r for r in report.records
            if r.name == "astrbot_plugin_private_companion"
        ).instance

        # 🔴 **判据的观察点：包一层入站入口计数**（2026-10-19 改）
        #
        # ## 为什么不能再用 `inst.data["users"]`
        #
        # 本判据原来读 `inst.data["users"][uid]["umo"]` —— 那是**陪伴 6.7.0
        # 的内部数据结构**。7.0.0 把它整个换掉了：
        #   * 实例上**根本没有 `data` 属性**（实测 `dir()` 零命中）；
        #   * 数据落进 `companion-7/companion-v2.sqlite3`（V2 存储，
        #     `astrbot_host/storage_location.py:24` 的 `V2_SUBDIRECTORY`）；
        #   * 且**未注册 Bot 账号与人格时完全不落盘**（`host_mappings_required`，
        #     `astrbot_host/multi_settings.py:167`）—— 实测派发后临时目录里
        #     只有 config/*.json，没有任何 sqlite。
        # ⇒ "查 users 里有没有 umo" 这个观察点在 7.0.0 下**不存在**。
        #
        # ## 但判据要守的那件事没变
        #
        # 守的是「**流式那条路也把消息派发给了插件**」（2026-09-26 那个真 bug：
        # 流式分支完全没有派发，而假宿主判据全绿）。7.0.0 的入站入口是
        # `receive_companion_message`（`main.py:438`，`@filter` 注册在
        # AdapterMessageEvent 上）⇒ **它有没有被调用**就是"派发了没有"的
        # 唯一直接证据，且与插件的内部存储模型无关。
        inbound_hits: list[str] = []

        # ⚠️ **必须包注册表里的那个 handler，不能包实例方法**（2026-10-19 踩到）
        #
        # 加载器在注册时就把方法绑成了
        # `functools.partial(Plugin.receive_companion_message, <实例>)`
        # 存进 `star_handlers_registry`（照真 AstrBot
        # `star_manager.py:1273-1299`）。派发走的是**注册表里那份 partial**，
        # 所以我第一版 `inst.receive_companion_message = _counting` 永远收不到调用
        # —— 判据于是恒定报"未派发"，把**判据自己的错**说成产品 bug。
        from astrbot_host.registry import star_handlers_registry

        _patched = []

        def _install_counter() -> int:
            """给 `receive_companion_message` 的注册表条目装计数器，返回装了几个。"""
            hits_installed = 0
            for h in star_handlers_registry:
                if getattr(h, "handler_name", "") != "receive_companion_message":
                    continue
                if getattr(h, "handler_module_path", "") != (
                    "astrbot_plugin_private_companion.main"
                ):
                    continue
                original = h.handler

                async def _counting(event, *a, __orig=original, **kw):
                    try:
                        umo = str(getattr(event, "unified_msg_origin", "") or "")
                    except Exception:  # noqa: BLE001 - 取 umo 失败不该影响派发
                        umo = ""
                    inbound_hits.append(umo)
                    return await __orig(event, *a, **kw)

                h.handler = _counting
                _patched.append((h, original))
                hits_installed += 1
            return hits_installed

        assert _install_counter() >= 1, (
            "注册表里找不到 `receive_companion_message` 的 handler —— "
            "7.0.0 的入站入口改名了？判据的观察点需要跟着更新"
        )

        svc.host = object()          # 非 None 即可（集成层只判空）
        svc.sessions = None          # ⚠️ 由 app 装配时再注入（见下）

        # 2) 真 app（真路由 → 真 ChatService）
        cfg = Config()
        cfg.auth.access_token = token
        cfg.storage.sqlite_path = str(tmp_path / "real.db")
        cfg.upstream.model = "m1"

        store = SqliteStore(cfg.storage.sqlite_path)
        await store.open()
        sessions = SessionRepository(store)
        chat = ChatService(cfg, _NoopUpstream())
        chat.bind_sessions(sessions)
        app = create_app(cfg, store, AuthManager(access_token=token), chat, sessions=sessions)
        app.config["EMOTION_PLUGIN_HOST"] = svc
        app.config["EMOTION_CONFIG_PROFILE_PATH"] = str(tmp_path / "p.json")
        app.config["EMOTION_PROVIDERS_PATH"] = str(tmp_path / "pr.json")

        c = app.test_client()
        await c.post("/api/v1/auth/login", json={"token": token})

        # ⚠️ `users` **每轮现取**（不提前抓一份存着）。
        #    ⚠️ 更正一条我先前写错的注释：我原以为"插件懒建 `data['users']`，
        #       提前抓会拿到另一个空字典"。**实测推翻**——
        #       `captured is live` 恒为 True（是同一个对象，插件的写入能被看到）。
        #       所以这不是"懒建"，现取只是**不受实例/时序变化影响**的稳妥写法。
        created: dict[str, str] = {}
        for label, stream in (("s", True), ("n", False)):
            before_count = len(inbound_hits)
            sid = f"sess-real-{label}"
            uid = f"e2e_stream_{label}"
            await sessions.create(session_id=sid, title=sid, user_id=uid)
            resp = await c.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "打个招呼"}],
                    "session_id": sid,
                    "stream": stream,
                },
            )
            if stream:
                await resp.get_data()          # 必须消费掉，否则生成器不跑
            # 🔴 改读**入站入口的调用记录**（见上面 `_counting_receive` 的说明）：
            #    只认"这一轮新发生的"调用，用长度差做差分。
            hits_now = inbound_hits[before_count:]
            created[label] = (
                hits_now[0] if hits_now else f"<未派发 status={resp.status_code}>"
            )

        await store.close()
        return created, inbound_hits, PLUGIN_TAKEOVER_PLACEHOLDER

    seen, hits, _placeholder = asyncio.run(run())
    return seen, hits


class _NoopUpstream:
    """假上游：**永远不该被调用**（插件会接管 `打个招呼`？——不会，所以允许它回话）。

    ⚠️ 我们不假设插件会不会接管：两条路的判据都只落在
       "**插件入站入口有没有被调用**"（`receive_companion_message`），
       而上游回什么都无所谓 —— 那正是差分实验能成立的原因。
    """

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def chat(self, messages, *, model="", **kw):  # noqa: ANN001
        from emotion_app.domain.upstream import ChatResult

        return ChatResult(content="上游回复", model=model or "m1", finish_reason="stop")

    async def chat_stream(self, messages, *, model="", **kw):  # noqa: ANN001
        from emotion_app.domain.upstream import StreamChunk

        yield StreamChunk(delta="上游回复")
        yield StreamChunk(done=True)


def test_real_plugin_gets_dispatched_by_streaming_chat(tmp_path):
    """🔴 **真机判据**：真插件 + 真 `/v1/chat/completions` + `stream: true` → 插件真的建档。

    见上方 `_real_plugin_stream_probe` 的长注释（为什么不许只用假宿主判据）。
    """
    seen, users = _real_plugin_stream_probe(tmp_path)
    assert seen.get("s", "").startswith("11:FriendMessage:"), (
        f"🔴 **流式聊天没有让插件建档** —— 插件里的 umo = {seen.get('s')!r}。"
        "面板永远走流式，这等于插件从没收到过用户的话（真 bug A）"
    )
    assert seen.get("n", "").startswith("11:FriendMessage:"), (
        f"非流式那条也没建档：{seen.get('n')!r}（这是对照，它本来就该是好的）"
    )
    assert seen["s"] != seen["n"], (
        f"两条路建了**同一个** umo（{seen['s']!r}）—— 说明派发没按会话取 user_id"
    )
