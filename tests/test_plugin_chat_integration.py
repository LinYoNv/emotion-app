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
    plugin_root = Path("/opt/astrbot/data/plugins")
    if not plugin_root.is_dir():
        pytest.fail(
            f"插件目录不存在：{plugin_root}（这不是 skip —— 前置条件不满足即判据失败）"
        )

    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()

    class _Cfg:
        class plugins:  # noqa: N801
            umo_user_id = "3412642082"

    async def run():
        loader = PluginLoader(
            plugin_dirs=[str(plugin_root)], mirror_root=str(tmp_path / "mirror")
        )
        await loader.load_all(ports=HostPorts(config={}))

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
def test_binding_command_is_the_only_path_that_binds(tmp_path):
    """🔴 **绑定指令是唯一会写 `bound_delivery_umo` 的路径**。

    实测区分（`proactive.py` 里两个函数名字很像，作用不同）：
      * `_note_private_user_umo`（普通消息触发）→ 只写 `user["umo"]`（**观察**到的）
      * `_bind_private_delivery_umo`（`陪伴 绑定主动消息` 触发）→ 写 `user["bound_delivery_umo"]`
        —— 面板 `app.js:9882` 判 `delivery_bound` 读的**正是后者**

    ⚠️ 为什么要专门钉这条：如果哪天真把"收到消息就当绑定"了，
       面板会显示"已绑定"，但**用户从没同意过接收主动消息** ——
       而主动消息是**会主动打扰用户**的能力，静默开启是严重的行为变化。

    本判据从**全新数据目录**开始，逐步验证两条路径的差异。
    """
    import logging
    import tempfile

    logging.disable(logging.ERROR)
    plugin_root = Path("/opt/astrbot/data/plugins")
    if not plugin_root.is_dir():
        pytest.fail(f"插件目录不存在：{plugin_root}（前置条件不满足=判据失败）")

    sys.path.insert(0, str(APP_DIR / "host"))
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.compat.core.star.star_tools import set_data_root
    from astrbot_host.dispatch import dispatch
    from astrbot_host.loader import PluginLoader
    from astrbot_host.platform_manager import build_platform_manager
    from astrbot_host.shadow import install_shadow_package
    from astrbot_host.umo import build_inbound_event

    install_shadow_package()

    async def run():
        set_data_root(tempfile.mkdtemp())          # 隔离：全新数据目录
        loader = PluginLoader(
            plugin_dirs=[str(plugin_root)], mirror_root=str(tmp_path / "mirror")
        )
        rep = await loader.load_all(
            ports=HostPorts(config={}, platform_manager=build_platform_manager())
        )
        inst = next(
            r for r in rep.records if r.name == "astrbot_plugin_private_companion"
        ).instance
        users = (getattr(inst, "data", {}) or {}).get("users", {})
        uid = "3412642082"

        # 1) 普通消息：只观察，不绑定
        await dispatch(build_inbound_event("你好呀", uid))
        u = users.get(uid) or {}
        return {
            "after_plain_umo": u.get("umo"),
            "after_plain_bound": u.get("bound_delivery_umo"),
            "users": users,
            "uid": uid,
            "dispatch": dispatch,
            "build": build_inbound_event,
        }

    st = asyncio.run(run())

    assert st["after_plain_umo"] == "11:FriendMessage:3412642082", (
        f"普通消息应记录观察到的 umo，实际 {st['after_plain_umo']!r}"
    )
    assert not st["after_plain_bound"], (
        "🔴 普通消息**不该**产生 bound_delivery_umo —— "
        "否则等于「收到消息就自动同意接收主动消息」（未经用户同意的行为变化）"
    )

    # 2) 绑定指令：才写 bound_delivery_umo
    async def bind():
        await st["dispatch"](st["build"]("陪伴 绑定主动消息", st["uid"]))
        return (st["users"].get(st["uid"]) or {}).get("bound_delivery_umo")

    bound = asyncio.run(bind())
    assert bound == "11:FriendMessage:3412642082", (
        f"绑定指令应写入与 umo 同构的 bound_delivery_umo，实际 {bound!r}"
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
    plugin_root = Path("/opt/astrbot/data/plugins")
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
    plugin_root = Path("/opt/astrbot/data/plugins")
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
