# -*- coding: utf-8 -*-
"""P0-2 / P0-3 判据：**插件指令真的被派发、真的产出回复**。

⭐ 这是"插件能在聊天里回话"的端到端验收 —— 前面所有兼容层工作都是为了这一刻。

设计原则（照项目铁律）：
  * 每个判据都能**失败**（下面每条都有对应的注入 bug 验过）；
  * 不伪造：没派发到就如实为空，不许"看起来成功"。
"""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))


@pytest.fixture
def probe_plugin(tmp_path, monkeypatch):
    """一个最小但**真实**的插件：用插件的真实写法注册指令。

    ⚠️ 关键点：handler 用 `async def cmd(self, event)` 定义，
       并且**通过包加载**（不是手工造 metadata）——
       这样 `functools.partial(instance)` 绑定、过滤器挂载、
       参数解析全都会真实发生。
    """
    pkg = tmp_path / "dispatch_probe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "metadata.yaml").write_text(
        "name: dispatch_probe\nversion: 1.0\nauthor: t\ndesc: t\n", encoding="utf-8"
    )
    (pkg / "main.py").write_text(
        "from astrbot.api.event import AstrMessageEvent, filter\n"
        "from astrbot.api.star import Context, Star, register\n"
        "\n"
        "@register('dispatch_probe', 't', 'probe', '1.0')\n"
        "class DispatchProbe(Star):\n"
        "    def __init__(self, context: Context):\n"
        "        super().__init__(context)\n"
        "        self.calls = []\n"
        "\n"
        "    @filter.command('回声')\n"
        "    async def echo_cmd(self, event: AstrMessageEvent, word: str = '默认'):\n"
        "        self.calls.append(('echo', word))\n"
        "        yield event.plain_result(f'收到:{word}')\n"
        "\n"
        "    @filter.command('静默')\n"
        "    async def silent_cmd(self, event: AstrMessageEvent):\n"
        "        self.calls.append(('silent', None))\n"
        "        event.stop_event()\n"
        "\n"
        "    @filter.command('返回串')\n"
        "    async def raw_str_cmd(self, event: AstrMessageEvent):\n"
        "        self.calls.append(('raw', None))\n"
        "        return '裸字符串'\n"
        "\n"
        "    @filter.command('直接发')\n"
        "    async def direct_send_cmd(self, event: AstrMessageEvent):\n"
        "        self.calls.append(('direct', None))\n"
        "        await event.send(event.plain_result('直接发送'))\n"
        "\n"
        "    @filter.command('炸')\n"
        "    async def boom_cmd(self, event: AstrMessageEvent):\n"
        "        self.calls.append(('boom', None))\n"
        "        raise RuntimeError('故意炸的')\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    for k in [k for k in list(sys.modules) if k.startswith("dispatch_probe")]:
        del sys.modules[k]

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.registry import star_handlers_registry
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    star_handlers_registry.clear()

    import tempfile

    with tempfile.TemporaryDirectory() as mirror:
        loader = PluginLoader(plugin_dirs=[str(tmp_path)], mirror_root=mirror)
        rep = asyncio.run(loader.load_all(ports=HostPorts(config={})))

    rec = next(r for r in rep.records if r.name == "dispatch_probe")
    assert rec.error == "", f"探针插件加载失败：{rec.error}\n{rec.traceback}"
    try:
        yield rec.instance
    finally:
        star_handlers_registry.clear()
        for k in [k for k in list(sys.modules) if k.startswith("dispatch_probe")]:
            del sys.modules[k]


def _event(text: str, *, private: bool = True):
    """造一条我们的入站消息事件（用真实的 AstrMessageEvent）。

    ⚠️ **umo 与 `is_private` 必须一致**（实测踩到）：
       真实现的 `is_private_chat()` 是从 **session**（由 umo 解析而来）推导的
       （`astr_message_event.py:255` → `self.session.message_type == FRIEND_MESSAGE`），
       **不看**构造参数。我第一版把 umo 写死成 `FriendMessage`，
       于是"群聊"用例实际拿到的是私聊 session → 唤醒判定必然为真 → 用例假失败。
       **umo 才是权威**，构造参数只是没有 session 时的退路。
    """
    from astrbot_host.compat.core.platform.astr_message_event import AstrMessageEvent
    from astrbot_host.compat.core.platform.astrbot_message import AstrBotMessage
    from astrbot_host.compat.core.platform.message_type import MessageType

    obj = AstrBotMessage()
    obj.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    obj.self_id = "bot"
    obj.session_id = "u1" if private else "g1"
    ev = AstrMessageEvent(
        message_str=text,
        message_obj=obj,
        # umo 第二段必须与 private 一致（平台实例 id 用 `11`，与生产同构）
        unified_msg_origin=(
            "11:FriendMessage:u1" if private else "11:GroupMessage:g1"
        ),
        sender_id="u1",
        platform_name="webchat",
        platform_id="11",
        is_private=private,
    )
    return ev


# ---------------------------------------------------------------------------
# 端到端：指令 → 执行 → 回复
# ---------------------------------------------------------------------------
def test_command_dispatches_and_produces_reply(probe_plugin):
    """⭐ 核心判据：`回声 你好` → 插件方法真的被调用 → 产出「收到:你好」。"""
    from astrbot_host.dispatch import dispatch

    ev = _event("回声 你好")
    res = asyncio.run(dispatch(ev))

    assert res.is_wake, "私聊 webchat 应被唤醒（真实现 waking_check 第 3 条）"
    assert [h.handler_name for h in res.activated] == ["echo_cmd"], (
        f"应命中且仅命中 echo_cmd，实际 {[h.handler_name for h in res.activated]}"
    )
    # 插件方法**真的被调用**了（这是"派发"与"只是选出 handler"的区别）
    assert probe_plugin.calls == [("echo", "你好")], f"插件未被正确调用：{probe_plugin.calls}"

    assert res.result is not None, "应产出结果"
    assert str(res.result) == "收到:你好", f"回复内容不对：{str(res.result)!r}"


def test_command_params_are_parsed_and_passed(probe_plugin):
    """指令参数要按签名解析并**传进** handler（GreedyStr/默认值同理）。"""
    from astrbot_host.dispatch import dispatch

    ev = _event("回声")
    res = asyncio.run(dispatch(ev))
    # 没给参数 → 用默认值
    assert probe_plugin.calls == [("echo", "默认")], probe_plugin.calls
    assert str(res.result) == "收到:默认"


def test_stop_event_breaks_propagation(probe_plugin):
    """`event.stop_event()` 要**真的阻断**后续（真实现靠 `is_stopped()` 逐轮检查）。"""
    from astrbot_host.dispatch import dispatch

    ev = _event("静默")
    res = asyncio.run(dispatch(ev))
    assert probe_plugin.calls == [("silent", None)]
    assert res.stopped is True
    # ⚠️ 注意 `allow_default_llm` 的**语义**：它是真实现那个三条件公式
    #    （`process_stage/stage.py:56-60`：`not _has_send_oper and
    #     is_at_or_wake_command and not call_llm`）——**本身不含 `is_stopped`**。
    #    真实现里"被终止就不继续"是由**调度器**保证的
    #    （`scheduler.py:56-60`：每 yield 一次就查 `is_stopped()` 并 break）。
    #    → 所以这里断言的是 `stopped=True`（真正的中断信号），
    #      而不是去改公式迎合直觉（我第一版就是那样写错的）。


def test_non_matching_command_does_not_run(probe_plugin):
    """不匹配的指令**不许**执行（防止"所有 handler 都跑一遍"这种假通过）。"""
    from astrbot_host.dispatch import dispatch

    ev = _event("不存在的指令")
    res = asyncio.run(dispatch(ev))
    assert probe_plugin.calls == [], f"不该被执行：{probe_plugin.calls}"
    assert res.activated == []


def test_raw_string_return_is_not_auto_wrapped(probe_plugin):
    """handler 返回**裸字符串**时，真实现**不**自动包装成结果。

    真实现 `context_utils.py:71-75`：只有 `MessageEventResult`/`CommandResult`
    才 `set_result`，其它（含 str）原样 yield 出去。
    ⚠️ 我们照抄这个语义 —— **不"顺手兼容"**，否则宿主行为与真实现分叉。
    """
    from astrbot_host.dispatch import dispatch

    ev = _event("返回串")
    res = asyncio.run(dispatch(ev))
    assert probe_plugin.calls == [("raw", None)]
    assert res.result is None, (
        f"裸字符串不该被自动包装成结果（真实现不包装），实际 {res.result!r}"
    )


def test_event_send_marks_has_send_oper(probe_plugin):
    """`event.send()` 之后 `_has_send_oper` 必须为 True（真实现 `:495`）。

    ⚠️ 插件靠它判断"这轮发出去没有"（**6 个文件**读它）：
       恒 False → 插件以为没发出去，可能重复发。
    """
    from astrbot_host.dispatch import dispatch

    ev = _event("直接发")
    res = asyncio.run(dispatch(ev))
    assert res.sent, "event.send() 的内容应被记录下来"
    assert ev._has_send_oper is True, "_has_send_oper 必须置 True"
    # 已经发过 → 不该再走默认 LLM 链路（真实现 process_stage:57）
    assert res.allow_default_llm is False


# ---------------------------------------------------------------------------
# 异常路径：必须响亮，不许静默
# ---------------------------------------------------------------------------
def test_handler_exception_is_reported_not_swallowed(probe_plugin):
    """handler 抛异常 → **记录 + 回给用户 + 终止事件**（照 `star_request.py:54-74`）。

    🔴 为什么不许静默：插件全挂而没人知道是最贵的 bug（项目铁律）。
    """
    from astrbot_host.dispatch import dispatch

    ev = _event("炸")
    res = asyncio.run(dispatch(ev))

    assert probe_plugin.calls == [("boom", None)], "handler 应先被执行"
    assert res.errors, "异常必须被记录（不许吞）"
    assert "故意炸的" in res.errors[0][1], res.errors
    assert res.stopped is True, "异常后应终止事件"
    # 真实现会把错误**回给用户**
    assert res.result is not None
    assert "出现异常" in str(res.result), f"应回错误给用户：{str(res.result)!r}"


# ---------------------------------------------------------------------------
# 唤醒语义
# ---------------------------------------------------------------------------
def test_no_wake_means_stopped_and_nothing_dispatched(probe_plugin):
    """群聊 + 无唤醒前缀 → **不唤醒** → 不派发 + `stop_event`。

    ⚠️ 这条对应真实现 `waking_check/stage.py:247-248`。
       私聊会无条件唤醒，所以必须用**群聊**才验得到这个分支。
    """
    from astrbot_host.dispatch import dispatch

    ev = _event("回声 你好", private=False)
    res = asyncio.run(dispatch(ev))
    assert res.is_wake is False, "群聊无唤醒前缀不该唤醒"
    assert res.stopped is True, "未唤醒应终止事件"
    assert probe_plugin.calls == [], "未唤醒不该执行任何 handler"


def test_wake_prefix_is_stripped_before_matching(probe_plugin):
    """唤醒前缀必须**被切掉**再匹配指令（真实现 `:123`）。

    ⚠️ 不切的话 `回声` 指令永远匹配不上 —— 这类 bug 表现为"指令没反应"，
       很难从现象反推。
    """
    from astrbot_host.dispatch import dispatch

    ev = _event("/回声 你好", private=False)
    res = asyncio.run(dispatch(ev, wake_prefixes=["/"]))
    assert res.is_wake is True
    assert ev.message_str == "回声 你好", f"前缀应被切掉，实际 {ev.message_str!r}"
    assert probe_plugin.calls == [("echo", "你好")], probe_plugin.calls


def test_activated_handlers_written_to_event_extra(probe_plugin):
    """`activated_handlers` / `handlers_parsed_params` 必须写回 event。

    🔴 真实现的 `process_stage/stage.py:33-35` 就是从这里取的 ——
       不写这两个 extra，后续阶段与插件都拿不到"这轮命中了谁"。
    """
    from astrbot_host.dispatch import dispatch

    ev = _event("回声 你好")
    asyncio.run(dispatch(ev))
    act = ev.get_extra("activated_handlers")
    assert act and [h.handler_name for h in act] == ["echo_cmd"]
    params = ev.get_extra("handlers_parsed_params")
    assert params, "handlers_parsed_params 应非空"
    # 指令参数要按 handler 全名归类（真实现同）
    assert any(v == {"word": "你好"} for v in params.values()), params


def test_handlers_without_filters_are_skipped(probe_plugin):
    """**零过滤器**的 handler 必须被跳过，绝不能"每条消息都触发"。

    🔴 这是真实现 `waking_check/stage.py:182-183` 的语义：
    ```python
    if len(handler.event_filters) == 0:
        continue          # ← 跳过，不是执行
    ```
    ⚠️ 我一开始把方向理解反了（以为"无过滤器 = 每条都触发"），
       实际是**永不执行** —— 这正是 BUG-1（子指令没挂 CommandFilter）
       导致 `prompt_tools` 16 个指令失效的原因。

    **为什么这条判据必须有**：注入"不跳过空过滤器"后，
    其余 10 条判据**全绿**（因为探针插件里没有零过滤器 handler）——
    说明那 10 条盖不住这个语义。补上这条后才抓得住。

    造法：直接往注册表塞一个**没有 event_filters** 的 handler
    （模拟"某个插件注册了却不带过滤器"的情形），
    然后在一次真实派发里确认它**没有被调用**。
    """
    import asyncio

    from astrbot_host.dispatch import dispatch
    from astrbot_host.registry import (
        EventType,
        StarHandlerMetadata,
        star_handlers_registry,
    )

    called = []

    async def naked_handler(event):
        called.append("naked")
        return None

    star_handlers_registry.append(
        StarHandlerMetadata(
            event_type=EventType.AdapterMessageEvent,
            handler_full_name="probe_naked_handler",
            handler_name="naked_handler",
            handler_module_path="dispatch_probe.main",
            handler=naked_handler,
            event_filters=[],          # ← 关键：零过滤器
        )
    )
    try:
        ev = _event("回声 你好")
        res = asyncio.run(dispatch(ev))
        assert called == [], (
            "零过滤器的 handler 被调用了 —— 真实现会跳过它"
            "（`waking_check/stage.py:182-183`），否则任何无过滤器 handler 都会响应每条消息"
        )
        assert all(h.handler_name != "naked_handler" for h in res.activated), (
            "零过滤器 handler 不该进 activated_handlers"
        )
        # 正常指令仍应工作（证明我们不是"整个派发都坏了"）
        assert probe_plugin.calls == [("echo", "你好")]
    finally:
        star_handlers_registry.remove_plugin("dispatch_probe")
