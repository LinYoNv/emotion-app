# -*- coding: utf-8 -*-
"""P1-2 / P1-3 判据：平台管理器 + `send_by_session` 投递。

⭐ 为什么需要这些判据（都是实测出来的真实风险）：

1. **插件按 `meta().id` 找平台**（`proactive_chat_runtime_bridge.py:551-553`），
   而 id 必须与 **umo 第一段逐字相同** —— 差一个字符就"找不到平台"，
   表现为"面板绑定看着成功、消息发不出去"。
2. **插件检查 `status == RUNNING`**（`:560`），非 RUNNING 直接拒绝发送。
3. 🔴 **投递端口没接时必须如实失败** —— 作者验收判据③明确要求
   「能力不对等要如实 degraded/uncertain，**不伪造 delivered**」。
   这条是本文件最重要的判据：一个"永远返回成功"的实现会让插件以为
   消息已送达，而用户什么都没收到。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))


class _RecordingDelivery:
    """记录投递内容的假端口（验证"真的交出去了"而不是"假装成功"）。"""

    def __init__(self, raises: Exception | None = None) -> None:
        self.sent: list[tuple[str, object]] = []
        self.raises = raises

    async def send(self, umo: str, chain: object) -> None:
        if self.raises is not None:
            raise self.raises
        self.sent.append((umo, chain))


# ---------------------------------------------------------------------------
# 一、管理器接口面（两条分支都要有）
# ---------------------------------------------------------------------------
def test_manager_exposes_both_platform_insts_and_get_insts():
    """`platform_insts` 与 `get_insts()` **都要有**，且指向同一份数据。

    🔴 实测确认：插件不同调用点分别用它们 ——
       `proactive_chat_runtime_bridge.py:546-547` 是 `get_insts()` 优先、
       `platform_insts` 兜底；而 `core_store.py:5629`、
       `platform_compat.py:214` **直接读** `platform_insts`。
       **只给一个会让另一条分支失效。**
    """
    from astrbot_host.platform_manager import build_platform_manager

    pm = build_platform_manager()
    assert hasattr(pm, "platform_insts"), "缺 platform_insts（有调用点直接读它）"
    assert callable(getattr(pm, "get_insts", None)), "缺 get_insts()"

    insts = pm.get_insts()
    assert list(insts) == list(pm.platform_insts), "两者应指向同一份数据"
    # 真实现 `manager.py:316-317` 就是 `return self.platform_insts`（同一对象）
    assert insts is pm.platform_insts, (
        "get_insts() 应返回**内部列表本身**（照真实现 manager.py:316），"
        "返回副本会让插件就地修改被静默丢弃"
    )


def test_platform_instance_identity_matches_umo_prefix():
    """实例 `meta().id == "11"`、`name == "webchat"` —— **不冒充 aiocqhttp**。"""
    from astrbot_host.platform_manager import build_platform_manager

    pm = build_platform_manager()
    inst = pm.get_insts()[0]
    meta = inst.meta()

    assert meta.id == "11", (
        f"平台实例 id 必须是 11（umo 第一段），实际 {meta.id!r} —— "
        "不一致会导致插件按 id 找不到平台"
    )
    assert meta.name == "webchat", (
        f"平台类型名应为 webchat（插件 platform_compat.py:29 把它当一等公民），"
        f"实际 {meta.name!r}"
    )
    assert meta.name != "aiocqhttp", "🔴 绝不冒充 aiocqhttp（伪造能力）"


def test_platform_status_is_running():
    """状态必须**真的**是 RUNNING —— 插件非 RUNNING 直接拒绝发送。"""
    from astrbot_host.compat.core.platform.platform import PlatformStatus
    from astrbot_host.platform_manager import build_platform_manager

    inst = build_platform_manager().get_insts()[0]
    assert inst.status is PlatformStatus.RUNNING, (
        f"状态应为 RUNNING（proactive_chat_runtime_bridge.py:560 会检查），"
        f"实际 {inst.status}"
    )


def test_manager_find_by_id_and_umo():
    """能按 id / umo 第一段找到实例；不存在的 id **找不到**（不许乱认）。"""
    from astrbot_host.platform_manager import build_platform_manager

    pm = build_platform_manager()
    assert pm.get_inst("11") is pm.get_insts()[0]
    assert pm.get_inst("webchat") is pm.get_insts()[0], "name 也应能匹配（照 platform_compat.py:220-224）"
    assert pm.find_by_umo("11:FriendMessage:3412642082") is pm.get_insts()[0]

    # 🔴 反例：不存在的 id 必须找不到 —— 否则会把消息发给错误的平台
    assert pm.get_inst("999") is None
    assert pm.find_by_umo("999:FriendMessage:x") is None
    assert pm.get_inst("") is None


# ---------------------------------------------------------------------------
# 二、投递语义（本文件的核心）
# ---------------------------------------------------------------------------
def test_send_by_session_delivers_through_port():
    """端口已注入 → 消息**真的交给端口**，并返回成功。"""
    from astrbot_host.compat.core.platform.message_session import MessageSession
    from astrbot_host.compat.core.platform.message_type import MessageType
    from astrbot_host.platform_manager import build_platform_manager

    delivery = _RecordingDelivery()
    inst = build_platform_manager(delivery=delivery).get_insts()[0]

    session = MessageSession("11", MessageType.FRIEND_MESSAGE, "3412642082")
    ok = asyncio.run(inst.send_by_session(session, "CHAIN"))

    assert ok is True, "端口接上了就该成功"
    assert delivery.sent == [("11:FriendMessage:3412642082", "CHAIN")], (
        f"消息没按 umo 交出去：{delivery.sent}"
    )


def test_send_by_session_fails_loudly_without_port():
    """🔴🔴 **不伪造 delivered**：端口未注入时必须**如实失败**。

    这是作者验收判据③的核心（「能力不对等要如实 degraded/uncertain，
    **不伪造 delivered**」）。

    ⚠️ 为什么这条最重要：一个"永远返回成功"的实现会让插件
       `_confirm_outbound_delivery(...)` 以为消息已送达（`proactive_message.py:17625`），
       进而**不再重试、也不提示失败** —— 用户什么都没收到，而系统里记的是"已发送"。
    """
    from astrbot_host.compat.core.platform.message_session import MessageSession
    from astrbot_host.compat.core.platform.message_type import MessageType
    from astrbot_host.platform_manager import build_platform_manager

    inst = build_platform_manager(delivery=None).get_insts()[0]
    session = MessageSession("11", MessageType.FRIEND_MESSAGE, "3412642082")

    result = asyncio.run(inst.send_by_session(session, "CHAIN"))
    assert result is not True, (
        "端口未注入却返回成功 = 伪造 delivered（违反作者验收判据③）"
    )
    # 插件的判定是 `if precise_result is not False:` → 必须返回**字面 False**
    # 才能触发它的降级路径（`proactive_message.py:17624-17631`）
    assert result is False, (
        f"应返回字面 False 以触发插件降级路径，实际 {result!r}"
    )


def test_send_by_session_swallows_port_error_but_reports_failure():
    """端口抛异常 → **不让异常穿出去**，但如实返回 False。

    ⚠️ 为什么吞异常：插件对两种失败的处理不同 ——
       抛异常走 `except`（`platform_send_exception`，**不再重试**），
       返回 False 会走"回退核心发送"（**再试 context.send_message**）。
       返回 False 对我们更有利；但**必须记 ERROR**（不许静默）。
    """
    from astrbot_host.compat.core.platform.message_session import MessageSession
    from astrbot_host.compat.core.platform.message_type import MessageType
    from astrbot_host.platform_manager import build_platform_manager

    delivery = _RecordingDelivery(raises=RuntimeError("上游通道断了"))
    inst = build_platform_manager(delivery=delivery).get_insts()[0]
    session = MessageSession("11", MessageType.FRIEND_MESSAGE, "u1")

    result = asyncio.run(inst.send_by_session(session, "CHAIN"))
    assert result is False, f"端口失败时应如实返回 False，实际 {result!r}"


# ---------------------------------------------------------------------------
# 三、Context 接线（插件实际拿到的东西）
# ---------------------------------------------------------------------------
def test_context_exposes_platform_manager():
    """`context.platform_manager` 必须是**属性**（真实现 `context.py:155`）。"""
    from astrbot_host.compat.core.star.context import Context, HostPorts
    from astrbot_host.platform_manager import build_platform_manager

    pm = build_platform_manager()
    ctx = Context(ports=HostPorts(platform_manager=pm))
    assert ctx.platform_manager is pm

    # 未注入时是 None（不是抛错）—— 插件对该分支有处理
    assert Context(ports=HostPorts()).platform_manager is None


def test_context_get_platform_inst_no_longer_hardcoded_none():
    """`get_platform_inst()` 应**查管理器**，不再硬编码返回 None。

    ⚠️ 这是我们原来的真实缺口（`return None`）——
       插件那条路必然早退，表现为"面板显示目标用户未配置"。
    """
    from astrbot_host.compat.core.star.context import Context, HostPorts
    from astrbot_host.platform_manager import build_platform_manager

    pm = build_platform_manager()
    ctx = Context(ports=HostPorts(platform_manager=pm))

    inst = ctx.get_platform_inst("11")
    assert inst is not None, "有平台管理器时 get_platform_inst('11') 不该返回 None"
    assert inst is pm.get_insts()[0]

    # 反例：不存在的 id 仍返回 None（不许乱认）
    assert ctx.get_platform_inst("999") is None
    # 没有管理器时返回 None（如实降级，不抛错）
    assert Context(ports=HostPorts()).get_platform_inst("11") is None


def test_capabilities_separates_manager_from_delivery():
    """能力诊断要把"有管理器"与"能投递"**分开**暴露。

    ⚠️ 只报 `platform_manager=True` 会让人以为"主动消息能用了"，
       而端口没接时其实发不出去 —— 那正是"看着像能用"的假象。
    """
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.platform_manager import build_platform_manager

    delivery = _RecordingDelivery()

    wired = HostPorts(platform_manager=build_platform_manager(delivery=delivery)).capabilities()
    assert wired["platform_manager"] is True
    assert wired["platform_delivery"] is True

    unwired = HostPorts(platform_manager=build_platform_manager(delivery=None)).capabilities()
    assert unwired["platform_manager"] is True, "管理器在"
    assert unwired["platform_delivery"] is False, (
        "端口没接时必须如实报 False —— 否则诊断说可用、实际发不出去"
    )

    none_caps = HostPorts().capabilities()
    assert none_caps["platform_manager"] is False
    assert none_caps["platform_delivery"] is False


# ---------------------------------------------------------------------------
# 四、模拟插件的真实查找逻辑（端到端等价）
# ---------------------------------------------------------------------------
def test_plugin_lookup_flow_finds_our_platform():
    """复刻插件的查找流程（`proactive_chat_runtime_bridge.py:545-566`），
    确认它能找到我们的平台并调用 `send_by_session`。

    ⚠️ 这里**逐行照插件写法**（get_insts 优先、platform_insts 兜底、
       按 meta().id 比、查 status），而不是"用我们的便捷方法" ——
       否则测的是我们的 API 而不是**插件实际会走的路径**。
    """
    from astrbot_host.compat.core.platform.message_session import MessageSession
    from astrbot_host.compat.core.platform.message_type import MessageType
    from astrbot_host.compat.core.platform.platform import PlatformStatus
    from astrbot_host.platform_manager import build_platform_manager

    delivery = _RecordingDelivery()
    manager = build_platform_manager(delivery=delivery)

    # ↓↓↓ 照抄插件逻辑 ↓↓↓
    platform_id = "11"
    getter = getattr(manager, "get_insts", None)
    platforms = (
        list(getter() or []) if callable(getter)
        else list(getattr(manager, "platform_insts", []) or [])
    )
    target_platform = next(
        (p for p in platforms if str(getattr(p.meta(), "id", "") or "") == str(platform_id)),
        None,
    )
    assert target_platform is not None, "插件按 id 找不到平台 → 会走兜底，主动消息走不到精确路径"
    assert getattr(target_platform, "status", PlatformStatus.RUNNING) == PlatformStatus.RUNNING, (
        "状态不是 RUNNING → 插件直接拒绝发送（reason=platform_not_running）"
    )
    session = MessageSession("11", MessageType.FRIEND_MESSAGE, "3412642082")
    result = asyncio.run(target_platform.send_by_session(session, "CHAIN"))
    # ↑↑↑ 照抄插件逻辑 ↑↑↑

    assert result is not False, "插件判据是 `is not False` —— 返回 False 会被当成失败"
    assert delivery.sent and delivery.sent[0][0] == "11:FriendMessage:3412642082"
