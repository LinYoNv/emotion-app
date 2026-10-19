# -*- coding: utf-8 -*-
"""判据：**工具执行链**（`_AgentRunner` 的多轮工具循环 + 插件工具接入）。

## 本文件守的那个真缺口（2026-09-22，实测 + 读真 AstrBot 源码坐实）

上一轮把"工具调用**能被看见**"修通了（`response_bridge` 统一转换，实测
`tool_called=True`）。**但那只是"模型说出了要调什么"** —— 从"说出来"到
"真的执行、结果回到模型"这一步是**完全断的**，且断在多个点上：

| # | 缺口 | 证据 |
|---|---|---|
| ① | `_AgentRunner` 只有 `run`/`run_stream` | 实测 `dir()` 只有这两个；缺 `step_until_done`/`done`/`get_final_llm_resp` |
| ② | `private_image.py:6555`、`proactive_message.py:4665` 用 `hasattr(runner,"step_until_done")` 守卫 | 缺方法 → **静默跳过整个工具循环**（零日志） |
| ③ | `private_image.py:6599`、`proactive_message.py:5188` 直接调 `runner.get_final_llm_resp()` | **无守卫** → `AttributeError` |
| ④ | 没有工具执行器 | 全仓无代码调用工具的 `handler` |
| ⑤ | `req.func_tool` 里没有插件注册的工具 | 真实现 `astr_main_agent.py:599-617` 有 merge，我们零调用 |
| ⑥ | 工具 handler 是**裸函数**（缺 `self`） | 真实现 `star_manager.py:1273-1299` 才绑插件实例 |
| ⑦ | `tool_calls_result` 被刻意不转发 | `provider.py` 的 `_IGNORED_TEXT_CHAT_KWARGS` |

🔴 **为什么这些特别贵**：②是 `hasattr` 守卫、⑤⑥是"静默 no-op"、
⑦是"参数被丢" —— **四种都是完全静默的降级**，读我们自己的源码
永远看不出来，必须真跑一次看它返回什么（本项目最贵的一课）。

## 判据设计原则

* **真跑**：`test_real_plugin_tool_loop_executes` 加载**真陪伴插件**
  （镜像目录，绝不动生产）并断言它的工具**真的被执行**；
* **每条缺口各有能失败的判据**（不是只测一个函数）——
  注入验证时逐个回退都能看到对应判据变红；
* **协议形状照真实现**：工具结果回填是
  `1 × role="assistant"（带 tool_calls）+ N × role="tool"（带 tool_call_id）`
  （真实现 `entities.py:74-79`、消费端 `openai_source.py:983-990`）。
"""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

#: 🔴 **插件源 = 仓库内 `plugins/`**（2026-10-19，情绪拍板）
#:
#: 原来指向 `/opt/astrbot/data/plugins` —— 别人 AstrBot 的生产目录：
#:   ① 违反 AGENTS.md 铁律「绝不碰 AstrBot」；
#:   ② 与 `tests/test_plugin_host_e2e.py`（读仓库内插件）**同名不同源**，
#:      同一个 pytest 进程里先后加载 ⇒ `sys.modules` 里留下旧版模块
#:      ⇒ 全量跑时 12 条判据互相污染（单独跑全绿）。
#: 换成本仓库的 6 个陪伴系插件后，判的是「我们的插件能不能被加载」。
PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins"
PLUGIN_NAME = "astrbot_plugin_private_companion"

#: 🔴 **7.0.0 里真正注册 LLM 工具的插件**（陪伴自己不再注册，见 fixture 说明）
TOOL_PLUGIN = "astrbot_plugin_memory_companion"
#: 用它其中一个工具做代表（原先用陪伴的 `pc_generate_photo`）
TOOL_NAME = "memory_companion_recall"
PLUGIN_DIR = PLUGIN_ROOT / PLUGIN_NAME


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _FakeChatResult:
    """照领域层 `ChatResult` 的真实字段（`emotion_app/domain/upstream.py:86`）。"""

    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.model = "fake-model"
        self.finish_reason = "stop"
        self.usage: dict = {}
        self.reasoning_content = ""
        self.raw: dict = {}
        self.tool_calls = tool_calls or []
        self.attachments: list = []


def _tool_call(name: str, args: str = "{}", call_id: str = "call_1") -> dict:
    """OpenAI 形状的 tool_call（`arguments` 是 **JSON 字符串**）。"""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class _ScriptedLLM:
    """按脚本逐轮返回的 LLM 端口替身。

    ⚠️ 用**脚本**而不是"永远返回同一个" —— 工具循环的判据必须能区分
       "第一轮要求调工具" 与 "第二轮拿到结果后的最终回复"，
       否则"循环只跑了一轮"也能假通过。
    """

    def __init__(self, script: list[_FakeChatResult]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            return _FakeChatResult(content="（脚本用尽）")
        return self.script.pop(0)


def _runner(script, *, func_tool=None, event=None):
    from astrbot_host.compat.core.astr_main_agent import (
        MainAgentBuildConfig,
        _AgentRunner,
    )
    from astrbot_host.compat.api.provider import ProviderRequest

    req = ProviderRequest(prompt="画张猫", func_tool=func_tool)
    llm = _ScriptedLLM(script)
    return (
        _AgentRunner(
            llm=llm,
            req=req,
            config=MainAgentBuildConfig(),
            event=event,
        ),
        llm,
        req,
    )


# ---------------------------------------------------------------------------
# ① runner 的方法面（插件按这个面调用；缺了就是静默跳过 / AttributeError）
# ---------------------------------------------------------------------------


def test_runner_exposes_the_api_the_plugin_calls():
    """⭐ 插件实测调用面必须齐（`hasattr` 守卫那条尤其重要）。

    `private_image.py:6555` / `proactive_message.py:4665` 用
    `hasattr(runner, "step_until_done")` 守卫 —— **没有就静默跳过整条工具循环**；
    `:6599` / `:5188` 则**无守卫**地调 `get_final_llm_resp()`。
    """
    from astrbot_host.compat.core.astr_main_agent import _AgentRunner

    for name in ("step", "step_until_done", "done", "get_final_llm_resp"):
        assert hasattr(_AgentRunner, name), (
            f"_AgentRunner 缺 {name}() —— 插件会静默跳过工具循环或抛 AttributeError"
        )


def test_get_final_llm_resp_is_callable_before_and_after_run():
    """`get_final_llm_resp()` 可调（返回 None 也比 AttributeError 好）。"""
    script = [_FakeChatResult(content="你好呀")]
    runner, _, _ = _runner(script)
    assert runner.get_final_llm_resp() is None
    asyncio.run(runner.run())
    assert runner.get_final_llm_resp() is not None
    assert runner.get_final_llm_resp().completion_text == "你好呀"


# ---------------------------------------------------------------------------
# ② 循环真的执行工具（原来"说出来就结束了"）
# ---------------------------------------------------------------------------


def test_tool_loop_executes_the_tool_and_feeds_result_back():
    """⭐⭐ 核心判据：工具**被真的执行**，结果**回到模型**，循环再跑一轮。"""
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    seen: list[dict] = []

    async def handler(event, prompt: str = "", kind: str = ""):
        seen.append({"event": event, "prompt": prompt, "kind": kind})
        return '{"status": "ok", "path": "/tmp/cat.png"}'

    tool = FunctionTool(
        name="pc_generate_photo",
        description="生成图片",
        parameters={
            "type": "object",
            "properties": {"prompt": {"type": "string"}, "kind": {"type": "string"}},
            "required": ["prompt"],
        },
        handler=handler,
    )
    tool_set = ToolSet([tool])
    script = [
        _FakeChatResult(
            tool_calls=[_tool_call("pc_generate_photo", '{"prompt": "猫", "kind": "selfie"}')]
        ),
        _FakeChatResult(content="画好啦，给你看～"),
    ]
    sentinel_event = object()
    runner, llm, req = _runner(script, func_tool=tool_set, event=sentinel_event)

    async def drive():
        return [r async for r in runner.step_until_done(5)]

    out = asyncio.run(drive())

    # 1) 工具真的被执行了，且 event 作为第一个位置参数传进去了
    assert len(seen) == 1, f"工具没有被执行（seen={seen}）"
    assert seen[0]["event"] is sentinel_event, "handler 的 event 参数不对"
    assert seen[0]["prompt"] == "猫" and seen[0]["kind"] == "selfie"

    # 2) 循环跑了**两轮**（第二轮才拿到最终文本）—— 只跑一轮就是"结果没回填"
    assert len(llm.calls) == 2, f"工具循环没有回到模型（LLM 调用 {len(llm.calls)} 次）"

    # 3) 第二轮带上去了工具结果消息，形状照真实现
    second = llm.calls[1]["messages"]
    assistant_msgs = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert assistant_msgs, "缺 role=assistant（带 tool_calls）那条消息"
    assert tool_msgs, "缺 role=tool 的结果消息"
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "/tmp/cat.png" in tool_msgs[0]["content"]
    # arguments 必须是 **JSON 字符串**（给 dict 部分上游会 400）
    assert isinstance(assistant_msgs[0]["tool_calls"][0]["function"]["arguments"], str)

    # 4) 最终回复是第二轮那条
    assert runner.get_final_llm_resp().completion_text == "画好啦，给你看～"
    assert runner.done() is True
    assert out, "step_until_done 应当有产出"


def test_tool_loop_stops_after_max_steps_and_strips_tools():
    """达上限时**拔掉工具**再跑最后一步（照真实现 `:1071-1087`）。

    ⚠️ 不拔工具的话模型会无限要求调工具（真实现专门处理了这个）。
    """
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    calls = {"n": 0}

    async def handler(event, **kw):
        calls["n"] += 1
        return "ok"

    tool = FunctionTool(
        name="loop_forever",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    tool_set = ToolSet([tool])
    # 脚本里每一轮都要调工具 —— 靠 max_step 收敛
    script = [_FakeChatResult(tool_calls=[_tool_call("loop_forever")]) for _ in range(10)]
    runner, llm, req = _runner(script, func_tool=tool_set)

    async def drive():
        return [r async for r in runner.step_until_done(3)]

    asyncio.run(drive())

    # 3 步上限 → 3 次进循环；随后"拔工具 + 注入提示词 + 再跑一步" = 第 4 次
    assert len(llm.calls) == 4, f"上限处理不符（LLM 调用 {len(llm.calls)} 次）"
    assert req.func_tool is None, "达上限后必须**拔掉全部工具**"
    last_msgs = llm.calls[-1]["messages"]
    assert any("maximum number of steps" in str(m.get("content", "")) for m in last_msgs), (
        "必须注入 MAX_STEPS_REACHED_PROMPT"
    )


def test_unknown_tool_is_backfilled_as_error_not_crash():
    """工具找不到 → 错误**作为工具结果回填**（照真实现 `:1156-1159`），不抛。"""
    from astrbot_host.compat.core.agent.tool import ToolSet

    script = [
        _FakeChatResult(tool_calls=[_tool_call("no_such_tool")]),
        _FakeChatResult(content="那我换个说法"),
    ]
    runner, llm, _ = _runner(script, func_tool=ToolSet([]))

    async def drive():
        return [r async for r in runner.step_until_done(4)]

    asyncio.run(drive())

    tool_msgs = [
        m for m in llm.calls[1]["messages"] if m.get("role") == "tool"
    ]
    assert tool_msgs, "未找到工具时也必须回填一条 tool 结果"
    assert "not found" in tool_msgs[0]["content"]
    assert "Available tools are" in tool_msgs[0]["content"]


def test_tool_handler_exception_is_logged_and_backfilled(caplog):
    """工具**抛异常** → 记 ERROR **且**把错误回填（两条都要：不静默、不整轮崩）。"""
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    async def boom(event, **kw):
        raise RuntimeError("生图后端炸了")

    tool = FunctionTool(
        name="explode",
        parameters={"type": "object", "properties": {}},
        handler=boom,
    )
    script = [
        _FakeChatResult(tool_calls=[_tool_call("explode")]),
        _FakeChatResult(content="抱歉，画不出来"),
    ]
    # ⚠️ 必须给 event：真实现的调用形状是 `handler(event, ...)`
    #    （`astr_agent_tool_exec.py:738`），生产里插件**总是**传 event
    #    （`build_main_agent(event=...)`）。不传 event 是"另一条路"，不是这条判据要测的。
    runner, llm, _ = _runner(script, func_tool=ToolSet([tool]), event=object())

    async def drive():
        return [r async for r in runner.step_until_done(4)]

    with caplog.at_level("ERROR"):
        asyncio.run(drive())

    assert any("explode" in r.message and "执行失败" in r.message for r in caplog.records), (
        "工具异常必须记 ERROR（静默失败比报错贵）"
    )
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "生图后端炸了" in tool_msgs[0]["content"], "异常也要回填给模型"


def test_tool_args_filtered_by_schema():
    """参数按 schema `properties` 过滤（照真实现 `:1169-1176`），并记 WARNING。"""
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    got: list[dict] = []

    async def handler(event, prompt: str = ""):
        got.append({"prompt": prompt})
        return "ok"

    tool = FunctionTool(
        name="t",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}},
        handler=handler,
    )
    script = [
        _FakeChatResult(
            tool_calls=[_tool_call("t", '{"prompt": "猫", "bogus": 1}')]
        ),
        _FakeChatResult(content="done"),
    ]
    runner, _, _ = _runner(script, func_tool=ToolSet([tool]), event=object())

    async def drive():
        return [r async for r in runner.step_until_done(4)]

    asyncio.run(drive())
    assert got == [{"prompt": "猫"}], f"非期望参数没有被过滤：{got}"


def test_missing_handler_is_reported_not_silently_ok():
    """无 handler 的工具 → 如实回填"无法执行"，**不假装成功**。"""
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    tool = FunctionTool(name="no_handler", parameters=None, handler=None)
    script = [
        _FakeChatResult(tool_calls=[_tool_call("no_handler")]),
        _FakeChatResult(content="ok"),
    ]
    runner, llm, _ = _runner(script, func_tool=ToolSet([tool]))

    async def drive():
        return [r async for r in runner.step_until_done(3)]

    asyncio.run(drive())
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "no handler" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# ③ 兼容层形状（ToolSet / FunctionToolManager / ToolCallsResult）
# ---------------------------------------------------------------------------


def test_toolset_has_the_real_collection_api():
    """`ToolSet` 的集合 API 照真实现（`tool.py:344-360`）。"""
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    a = ToolSet([FunctionTool(name="a")])
    b = ToolSet([FunctionTool(name="b")])
    assert a.names() == ["a"]
    assert len(a) == 1 and bool(a) is True
    assert [t.name for t in a] == ["a"]
    a.merge(b)
    assert sorted(a.names()) == ["a", "b"]
    assert bool(ToolSet([])) is False, "空集必须为假（真实现靠它判有没有工具）"


def test_toolset_add_tool_replaces_and_keeps_active():
    """同名去重规则照真实现（`:91-108`）：active 覆盖；inactive 不覆盖 active。"""
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    ts = ToolSet([FunctionTool(name="x", active=True)])
    ts.add_tool(FunctionTool(name="x", active=False))
    assert ts.get_tool("x").active is True, "inactive 不该覆盖 active"
    ts.add_tool(FunctionTool(name="x", active=True))
    assert len(ts) == 1, "同名必须替换而不是追加"


def test_tool_manager_exposes_real_api_and_full_tool_set():
    """`FunctionToolManager` 的真实现方法面 + `get_full_tool_set()` 形状。"""
    from astrbot_host.registry import FunctionToolManager

    mgr = FunctionToolManager()
    mgr.add_func(
        "demo",
        [{"type": "string", "name": "q", "description": "查询"}],
        "示例工具",
        handler=lambda **kw: "hi",
    )
    for name in ("func_list", "get_tool", "remove_func", "get_full_tool_set", "names"):
        assert hasattr(mgr, name), f"FunctionToolManager 缺 {name}（真实现有）"
    assert [s.name for s in mgr.func_list] == ["demo"]
    assert mgr.get_tool("demo") is not None

    ts = mgr.get_full_tool_set()
    assert ts.names() == ["demo"], "get_full_tool_set 必须把注册的工具装进 ToolSet"
    tool = ts.get_tool("demo")
    # 参数必须是 **JSON Schema 形状**（真实现 `FunctionTool.parameters` 就是它），
    # 不是 add_func 传进去的那个 list
    assert tool.parameters["type"] == "object"
    assert "q" in tool.parameters["properties"]
    # active / handler_module_path 必须存在（真实现有；缺了 star_manager 的绑定逻辑没法照抄）
    assert hasattr(tool, "active") and hasattr(tool, "handler_module_path")

    mgr.remove_func("demo")
    assert mgr.names() == []


def test_tool_calls_result_serializes_like_real_impl():
    """`ToolCallsResult.to_openai_messages()` 形状照真实现（`entities.py:74-79`）。

    → `[1 × assistant（带 tool_calls）, N × tool]`。这是喂回上游的**协议形状**，
      给错上游就 400 或模型看不懂工具结果。
    """
    from astrbot_host.compat.core.provider.entities import ToolCallsResult

    info = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
        ],
    }
    tcr = ToolCallsResult(
        tool_calls_info=info,
        tool_calls_result=[{"role": "tool", "tool_call_id": "c1", "content": "结果"}],
    )
    msgs = tcr.to_openai_messages()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "assistant" and msgs[0]["tool_calls"]
    assert msgs[1]["role"] == "tool" and msgs[1]["tool_call_id"] == "c1"
    assert tcr.to_openai_messages_model() == [info, tcr.tool_calls_result[0]]


def test_provider_forwards_tool_calls_result_as_messages():
    """🔴 `text_chat(tool_calls_result=...)` **必须拼成消息喂回上游**，不许丢。

    原实现把它放在 `_IGNORED_TEXT_CHAT_KWARGS` 里（"刻意不转发"）——
    后果是**工具结果永远回不到模型**（模型只能重复调同一个工具）。
    真实现是 `openai_source.py:983-990`：`context_query.extend(tcr.to_openai_messages())`。
    """
    from astrbot_host.compat.core.provider.entities import ToolCallsResult
    from astrbot_host.provider import Provider, _iter_tool_calls_results

    tcr = ToolCallsResult(
        tool_calls_info={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ],
        },
        tool_calls_result=[{"role": "tool", "tool_call_id": "c1", "content": "42"}],
    )

    # 1) 归一化：三种形状都要认（真实现字段类型是 `list | 单个 | None`）
    assert _iter_tool_calls_results(tcr) == [tcr]
    assert _iter_tool_calls_results([tcr]) == [tcr]
    assert _iter_tool_calls_results(None) == []
    assert _iter_tool_calls_results([tcr, None]) == [tcr]

    # 2) 拼出来的消息形状必须是 assistant + tool 两条（喂回上游的协议形状）
    msgs: list[dict] = []
    for block in _iter_tool_calls_results(tcr):
        msgs.extend(block.to_openai_messages())
    assert [m["role"] for m in msgs] == ["assistant", "tool"]

    # 3) `tool_calls_result` **不许**在忽略集合里（被忽略 = 结果永远丢）
    from astrbot_host.provider import _IGNORED_TEXT_CHAT_KWARGS

    assert "tool_calls_result" not in _IGNORED_TEXT_CHAT_KWARGS, (
        "tool_calls_result 又被放回忽略集合了 —— 工具结果会回不到模型"
    )

    # 4) **真跑一次 text_chat**：断言上游收到的 messages 里真有那两条。
    #    ⚠️ 只测 `to_openai_messages` 不够 —— "函数对 ≠ 接线对"（本项目踩过）。
    captured: dict = {}

    class _LLM:
        async def generate(self, **kw):
            captured.update(kw)
            return _FakeChatResult(content="ok")

    provider = Provider(provider_id="probe", model="fake", llm=_LLM())
    asyncio.run(
        provider.text_chat(prompt="继续", tool_calls_result=tcr)
    )
    sent = captured.get("messages") or []
    roles = [m.get("role") for m in sent]
    assert "assistant" in roles and "tool" in roles, (
        f"上游收到的消息里没有工具结果：roles={roles}"
    )
    tool_msg = next(m for m in sent if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "c1" and tool_msg["content"] == "42"
    # prompt 仍然在最后（真实现语义：工具结果在前，本轮 prompt 在后）
    assert roles[-1] == "user"


def test_signature_mismatch_check_does_not_swallow_inner_typeerror():
    """宽 `except TypeError` 必须只认"参数不匹配"，不许吞 handler 内部的错。

    这是本项目踩过的坑（错误信息会撒谎：一个 bug 把另一个 bug 的现场盖掉）。
    """
    from astrbot_host.compat.core.astr_main_agent import _looks_like_signature_mismatch

    assert _looks_like_signature_mismatch(
        TypeError("f() got an unexpected keyword argument 'prompt'"), {"prompt": 1}
    )
    assert _looks_like_signature_mismatch(
        TypeError("f() missing 1 required positional argument: 'event'"), {}
    )
    # ⚠️ handler 内部抛的 TypeError **不许**被当成签名问题
    assert not _looks_like_signature_mismatch(
        TypeError("unsupported operand type(s) for +: 'int' and 'str'"), {"prompt": 1}
    )


def test_handler_inner_typeerror_propagates_and_is_backfilled():
    """handler 内部抛 TypeError → 走"执行失败"路径（记 ERROR + 回填），不当签名问题。

    ⚠️ **为什么这里必须用【同步】handler**（第一版抓不住注入，实测两次才找准）：

    `_invoke_tool_handler` 的兜底重试只包住**调用那一刻**：

    ```python
    try:
        result = handler(event, **args)     # ← async handler 在这里只返回 coroutine
    except TypeError:
        ...重试 handler(**args)
    if inspect.isawaitable(result):
        return await result                 # ← 协程体里的 TypeError 在这里才炸（try 之外）
    ```

    * **async handler**：函数体里的 TypeError 在 `await` 时抛，**根本进不了 except**
      → 注入"无条件吞"也看不出区别（第一版就是这么假绿的）。
    * **sync handler**：函数体里的 TypeError 就在 `handler(...)` 里抛 → **进 except** →
      这才真正考到"该不该重试"。

    ✅ 判据用**调用次数**区分（*args/**kwargs 让两种调法都能进函数体）：

    | | 正确 | 注入"无条件吞 TypeError" |
    |---|---|---|
    | handler 调用次数 | **1** | **2**（多了一次重试） |

    ⚠️ 顺带记录一条**产品事实**：async handler 的体内异常本来就绕过了这层兜底
       （在 `await` 处抛），所以那个"宽 except 吞错"的风险**只对同步 handler 成立**。
    """
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    calls = {"n": 0}

    def handler(*args, **kwargs):  # 同步：体内异常会进 except，才能考到兜底逻辑
        calls["n"] += 1
        return 1 + "x"  # TypeError：函数**内部**的错，不是签名不匹配

    tool = FunctionTool(
        name="inner_boom",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    script = [
        _FakeChatResult(tool_calls=[_tool_call("inner_boom")]),
        _FakeChatResult(content="ok"),
    ]
    runner, llm, _ = _runner(script, func_tool=ToolSet([tool]), event=object())

    async def drive():
        return [r async for r in runner.step_until_done(3)]

    asyncio.run(drive())
    assert calls["n"] == 1, (
        f"handler 被调用了 {calls['n']} 次 —— >1 说明内部 TypeError 被误当成"
        "「签名不匹配」并触发了重试（宽 except 把真因盖掉）"
    )
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "execution failed" in tool_msgs[0]["content"]


def test_async_handler_inner_error_still_reaches_execution_failed():
    """async handler 体内异常同样要变成"执行失败"回填（不许整轮崩）。

    ⚠️ 与上一条的区别：async 的异常在 `await` 处抛（**绕过**签名兜底），
       所以这条守的是"上层 `_execute_tools` 必须接住"这一层 —— 两层都要有判据。
    """
    from astrbot_host.compat.core.agent.tool import FunctionTool, ToolSet

    async def handler(*args, **kwargs):
        raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

    tool = FunctionTool(
        name="async_boom",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    script = [
        _FakeChatResult(tool_calls=[_tool_call("async_boom")]),
        _FakeChatResult(content="ok"),
    ]
    runner, llm, _ = _runner(script, func_tool=ToolSet([tool]), event=object())

    async def drive():
        return [r async for r in runner.step_until_done(3)]

    asyncio.run(drive())
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "execution failed" in tool_msgs[0]["content"], (
        "async handler 的体内异常没有被接住并回填"
    )


def test_build_main_agent_wires_plugin_tools_into_the_request():
    """⭐⭐ **接线判据**：`build_main_agent()` 必须真的把插件工具并进 `req.func_tool`。

    🔴 **为什么单独要这一条**（注入验证抓到的判据盲区）：
       `test_real_plugin_tools_merge_into_request` 是**直接调** `_merge_plugin_tools(...)`
       —— 它只证明"函数对"，**不证明"函数被接上了"**。
       实测：把 `build_main_agent` 里那行调用删掉，那条判据**照样绿**。

    这正是本项目反复踩的那类盲区（**函数对 ≠ 接线对**，上一轮同类问题出现过两次）。

    ⚠️ **判据自带工具表**（不依赖全局 `llm_tools` 里有没有真插件）：
       全局表是进程级单例，别的测试文件加载过插件它才有内容 ——
       依赖它就变成"看运行顺序"的脆弱判据（实测：单跑本文件时它是空的）。
       这里用**自己的 manager** 注入上下文，既不污染全局表，也与顺序无关。
    """
    from astrbot_host.compat.core.astr_main_agent import (
        MainAgentBuildConfig,
        build_main_agent,
    )
    from astrbot_host.compat.api.provider import ProviderRequest
    from astrbot_host.registry import FunctionToolManager

    mgr = FunctionToolManager()
    mgr.add_func(
        "probe_tool",
        [{"type": "string", "name": "q", "description": "查询"}],
        "接线判据用的探针工具",
        handler=lambda **kw: "ok",
    )

    class _Ports:
        llm = _ScriptedLLM([_FakeChatResult(content="ok")])

    class _Ctx:
        _ports = _Ports()

        def get_llm_tool_manager(self):
            return mgr

    class _Event:
        unified_msg_origin = "11:FriendMessage:default"

        def get_extra(self, key, default=None):
            return default

        def set_extra(self, key, value):
            setattr(self, key, value)

    req = ProviderRequest(prompt="画张猫")
    assert req.func_tool is None, "前置：本轮请求本来没有工具"

    result = asyncio.run(
        build_main_agent(
            event=_Event(),
            plugin_context=_Ctx(),
            config=MainAgentBuildConfig(),
            req=req,
        )
    )
    assert result is not None, "build_main_agent 应当返回构建结果"
    # ⭐ 真正要断言的：**经过 build_main_agent 之后** req.func_tool 被填上了
    assert req.func_tool is not None, (
        "build_main_agent 没有把插件工具并进 req.func_tool —— "
        "模型会一个工具都看不到（插件自己只做删、不做加）"
    )
    assert "probe_tool" in req.func_tool.names(), (
        f"并进来的工具不对：{req.func_tool.names()}"
    )
    # 且 runner 拿到的就是这份（不是另建一份空的）
    assert result.agent_runner._req is req
    assert result.agent_runner._tools(), "runner 交上游的 tools 不该为空"


# ---------------------------------------------------------------------------
# ④ 真插件：工具 handler 绑定了实例（缺了执行必错）
# ---------------------------------------------------------------------------


pytestmark_real = pytest.mark.skipif(
    not PLUGIN_DIR.is_dir(),
    reason=f"真插件不存在：{PLUGIN_DIR}",
)


@pytest.fixture(scope="module")
def loaded_real_plugin(tmp_path_factory):
    """加载**真陪伴插件**一次（镜像目录 —— 绝不动生产目录）。"""
    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    # 🔴 **2026-10-19：改加载 `memory_companion`**
    #
    # 陪伴 7.0.0 **不再自己注册 LLM 工具**（改由拓展插件提供，见
    # `test_loader_real_plugin.py::test_llm_tools_registered_with_schema`）。
    # 而本文件要验的是「**工具循环机制**」：handler 有没有绑实例、
    # 工具会不会并进 `req.func_tool`、重载后会不会重绑。
    # ⇒ 换成 7.0.0 里**真正注册工具**的插件（记忆插件实测 11 个工具）。
    from conftest import shared_mirror_root
    mirror = shared_mirror_root()
    loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT], mirror_root=mirror)
    report = asyncio.run(
        loader.load_all(ports=HostPorts(config={}), only=[TOOL_PLUGIN])
    )
    assert report.records, "加载器没有发现真插件"
    return report


@pytestmark_real
def test_real_plugin_tool_handler_is_bound_to_instance(loaded_real_plugin):
    """⭐ 真插件的工具 handler 必须**已绑定插件实例**（否则执行必 `TypeError`）。

    真实现靠 `star_manager.py:1273-1299` 把
    `ft.handler` 换成 `functools.partial(raw, 插件实例)`。
    我们漏了这一步时的实测症状：`get_func(TOOL_NAME).handler`
    是**裸函数**，签名 `(self, event, ...)` —— 缺 `self`。
    """
    from astrbot_host.registry import llm_tools

    spec = llm_tools.get_func(TOOL_NAME)
    assert spec is not None, f"插件应当注册了 {TOOL_NAME}"
    assert isinstance(spec.handler, functools.partial), (
        "工具 handler 没有绑定插件实例 —— 调用时必然缺 self（TypeError）"
    )
    assert spec.handler_module_path == f"{TOOL_PLUGIN}.main"
    assert getattr(spec, "active", None) is True


@pytestmark_real
def test_real_plugin_tools_merge_into_request(loaded_real_plugin):
    """⭐ `build_main_agent` 必须把**真插件的工具**并进 `req.func_tool`。

    真实现 `astr_main_agent.py:599-617`；缺它时插件自己那些
    "按会话裁剪/标注工具" 的逻辑（`main.py:18914`/`:18964`/`:18254`）
    全部静默 no-op，模型一个工具都看不到。
    """
    from astrbot_host.compat.core.astr_main_agent import _merge_plugin_tools
    from astrbot_host.compat.api.provider import ProviderRequest
    from astrbot_host.registry import llm_tools

    class _Ctx:
        def get_llm_tool_manager(self):
            return llm_tools

    req = ProviderRequest(prompt="画张猫")
    assert req.func_tool is None
    n = _merge_plugin_tools(req, _Ctx())

    # 🔴 2026-10-19：记忆插件实测注册 11 个工具（陪伴 7.0.0 自己注册 0 个）
    assert n >= 10, f"并进来的工具太少（{n}）—— 记忆插件实测注册 11 个"
    assert req.func_tool is not None
    names = req.func_tool.names()
    assert TOOL_NAME in names, f"缺少核心工具：{names[:10]}"


@pytestmark_real
def test_real_plugin_tool_loop_really_executes(loaded_real_plugin):
    """⭐⭐⭐ 端到端：**真插件**的工具经 `step_until_done` **真的被执行**。

    ⚠️ 这是本文件最重要的一条 —— 它把"读源码看不出来"的那一段跑起来：
      用真插件的工具表 + 脚本化 LLM，断言工具的 handler **真的被调用**
      （用一个包在真 handler 外的计数器观测，不依赖插件内部状态）。
    """
    from astrbot_host.compat.core.astr_main_agent import (
        MainAgentBuildConfig,
        _AgentRunner,
    )
    from astrbot_host.compat.api.provider import ProviderRequest
    from astrbot_host.registry import llm_tools

    #: 拿真工具，但把 handler 包一层计数器 —— 这样"有没有被执行"可观测，
    #: 又不依赖插件内部的生图运行时（那需要完整面板配置，属于另一层）。
    spec = llm_tools.get_func(TOOL_NAME)
    assert spec is not None
    raw_handler = spec.handler
    hits: list[tuple] = []

    async def counting_handler(*args, **kwargs):
        hits.append((args, kwargs))
        return '{"status": "captured", "must_not_claim_sent": true}'

    spec.handler = counting_handler
    try:
        class _Ctx:
            def get_llm_tool_manager(self):
                return llm_tools

        class _Event:
            unified_msg_origin = "11:FriendMessage:default"

        script = [
            _FakeChatResult(
                tool_calls=[
                    _tool_call(
                        TOOL_NAME,
                        '{"query": "窗边的猫"}',
                    )
                ]
            ),
            _FakeChatResult(content="画好啦"),
        ]
        req = ProviderRequest(prompt="画张猫")
        # 走真实入口：先并工具，再跑循环
        from astrbot_host.compat.core.astr_main_agent import _merge_plugin_tools

        _merge_plugin_tools(req, _Ctx())
        assert req.func_tool is not None and TOOL_NAME in req.func_tool.names()

        llm = _ScriptedLLM(script)
        runner = _AgentRunner(
            llm=llm,
            req=req,
            config=MainAgentBuildConfig(),
            event=_Event(),
        )

        async def drive():
            return [r async for r in runner.step_until_done(5)]

        asyncio.run(drive())

        assert hits, "真插件的工具**没有被执行** —— 工具循环没接上"
        # 记忆插件的 `recall` 工具参数是 `query`（陪伴的生图工具才是 prompt/kind）
        assert hits[0][1].get("query") == "窗边的猫"
        # 事件作为第一个位置参数（真实现 `astr_agent_tool_exec.py:738`）
        assert hits[0][0], "handler 的第一个位置参数（event）没传"
        # 结果回填 + 循环收尾
        tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
        assert tool_msgs and "captured" in tool_msgs[0]["content"]
        assert runner.get_final_llm_resp().completion_text == "画好啦"
    finally:
        spec.handler = raw_handler


@pytestmark_real
def test_loader_reports_llm_tool_binding_count(loaded_real_plugin):
    """加载报告要**如实暴露**绑定了几个工具（为 0 而插件有工具 = 执行必错）。"""
    rec = loaded_real_plugin.records[0]
    # 🔴 2026-10-19：≥15 → ≥10（记忆插件实测注册 11 个工具；陪伴 7.0.0 注册 0 个）
    assert getattr(rec, "llm_tool_bind_count", 0) >= 10, (
        f"llm_tool_bind_count={getattr(rec, 'llm_tool_bind_count', None)} —— "
        "记忆插件有 11 个工具，绑少了说明归属匹配写错"
    )


@pytestmark_real
def test_reload_rebinds_tools_to_the_new_instance(tmp_path_factory):
    """🔴 **重载后工具必须绑到【新】实例**（2026-09-22 实测抓到的真 bug）。

    ## 这个 bug 是什么（读我们自己的源码看不出来，跑两次 load 才现形）

    第 2 次加载时，`spec.handler` 已经是第 1 次留下的 `functools.partial`。
    而 **`functools.partial` 的 `__module__` 恒为 `'functools'`**
    （partial 自己类所在模块），**不是**被包函数的模块 →
    按 `handler.__module__` 判归属**永远匹配不上** → 整个绑定被跳过。

    实测对照（同进程连续 load 两次真陪伴插件）：

    | | 修前 | 修后 |
    |---|---|---|
    | 第 1 次 `bind_count` | 20 | 20 |
    | 第 2 次 `bind_count` | **0** | **20** |
    | 第 2 次 `handler_module_path` | **None** | `...main` |
    | 第 2 次绑的实例 | **旧实例**（partial 未更新） | 新实例 |

    ⚠️ **为什么这是产品 bug 而不是测试假象**：宿主支持重复 `load_all`（重载插件），
    重载后工具会拿**旧实例**执行 —— 静默用错对象（本项目最贵的那类）。
    而且它是**静默的**：`bind_count=0` 只出现在诊断里，线上没有一行报错。
    """
    import asyncio
    import functools as _ft

    from astrbot_host.compat.core.star.context import HostPorts
    from astrbot_host.loader import PluginLoader
    from astrbot_host.registry import llm_tools

    def _load_once(tag: str):
        from conftest import shared_mirror_root

        mirror = shared_mirror_root()
        loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT], mirror_root=mirror)
        return asyncio.run(
            loader.load_all(ports=HostPorts(config={}), only=[TOOL_PLUGIN])
        )

    first = _load_once("a")
    spec1 = llm_tools.get_func(TOOL_NAME)
    inst1 = spec1.handler.args[0]
    assert first.records[0].llm_tool_bind_count >= 10  # 记忆插件实测 11

    second = _load_once("b")
    rec2 = second.records[0]
    spec2 = llm_tools.get_func(TOOL_NAME)

    assert rec2.llm_tool_bind_count >= 10, (
        f"重载后绑定被跳过（bind_count={rec2.llm_tool_bind_count}）—— "
        "工具会继续用**旧实例**执行"
    )
    assert spec2.handler_module_path == f"{TOOL_PLUGIN}.main"
    assert isinstance(spec2.handler, _ft.partial)
    # ⚠️ 必须是**新实例**（旧实例 = 静默用错对象）
    assert spec2.handler.args[0] is not inst1, "重载后工具仍绑在【旧】插件实例上"
    # 且不许叠成 partial(partial(f, inst), inst)（照真是 idempotent 绑定）
    assert not isinstance(spec2.handler.func, _ft.partial), (
        "partial 叠了两层 —— 调用时参数会错位（比不绑更难查）"
    )
