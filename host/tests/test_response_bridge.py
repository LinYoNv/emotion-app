# -*- coding: utf-8 -*-
"""判据：模型响应转换（`response_bridge`）—— **工具调用与推理内容不许丢**。

## 本文件守的那个真 bug（2026-09-21，实测）

宿主有**三条**把上游返回值交给插件的路，原来自各拼了一个 `LLMResponse`：

```
① Provider.text_chat()                 host/astrbot_host/provider.py
② Context._as_llm_response()           compat/core/star/context.py
③ _AgentRunner.run()                   compat/core/astr_main_agent.py
```

三处都只搬了文本：

```python
resp = LLMResponse(completion_text=_extract_text(out))
```

🔴 **后果**：`tool_calls`（`ChatResult.tool_calls`）与 `reasoning_content`
**被静默丢弃**。插件读的是：

* `resp.tools_call_name` — `main.py:19298` / `:19363`、`llm_tool_actions.py:2474`、
  `daily_state.py:14764`、`hdsi_experiment.py:573`
* `resp.reasoning_content` — 多处
* `resp.usage` — `token_budget.py`

→ 插件永远认为"模型没调工具" ⇒ **它的 function calling 在我们的宿主上完全失效，
且一行日志都没有**（`getattr` 带默认值 = 静默降级）。

⚠️ 第 ③ 条更彻底：它用自己的 `_extract_reasoning`，读的是
`raw.choices[0].message.reasoning_content` —— 而我们的返回值是领域层
`ChatResult`（**没有 `.choices`**）→ **恒返回 None**，推理内容 100% 丢失。

## 判据设计原则

* **唯一入口**：`response_bridge.to_llm_response` 之外的任何地方
  **不许**再构造 `LLMResponse`（源码级扫描判据，`test_no_second_handbuilt_llm_response`）。
  这条是"同一概念多条写入路径"那个最贵教训的直接固化。
* **每条路各有判据**：三条路**分别**断言工具调用能通过（不是只测桥本身）——
  否则"我改好了桥但有一条路没接上"会漏掉（上一轮就踩过）。
* **对齐真实现**：`tools is None` 时不解析 tool_calls（`openai_source.py:881`）、
  有工具调用时 `role="tool"`（`:925`）、参数解析失败用 `{}` 不中断（`:900-903`）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST_DIR))

from astrbot_host.response_bridge import (  # noqa: E402
    extract_reasoning,
    extract_text,
    parse_tool_calls,
    to_llm_response,
)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _FakeChatResult:
    """照领域层 `ChatResult` 的真实字段（`emotion_app/domain/upstream.py:86`）。"""

    def __init__(
        self,
        content: str = "",
        tool_calls: list | None = None,
        reasoning_content: str = "",
        usage: dict | None = None,
        finish_reason: str = "stop",
    ) -> None:
        self.content = content
        self.model = "fake-model"
        self.finish_reason = finish_reason
        self.usage = usage if usage is not None else {}
        self.reasoning_content = reasoning_content
        self.raw: dict = {}
        self.tool_calls = tool_calls or []
        self.attachments: list = []


def _photo_tool_call(name: str = "pc_generate_photo", args: str = '{"prompt": "猫"}') -> dict:
    """一个 OpenAI 形状的 tool_call（`arguments` 是 **JSON 字符串**）。"""
    return {
        "id": "call_abc",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class _FakeToolSet:
    """工具集替身 —— 只要**不是 None** 就够了（真实现只判 `is not None`）。"""


# ---------------------------------------------------------------------------
# 1. 桥本身
# ---------------------------------------------------------------------------


def test_tool_calls_survive_conversion():
    """⭐ 核心判据：`ChatResult.tool_calls` 必须变成 `tools_call_*` 三个字段。

    这三条一起给才对 —— 插件按 **索引** 对齐它们
    （`entities.py:156-162` 用 `tools_call_ids[idx]` + `tools_call_name[idx]`
    拼回发往上游的消息），少一个就会 IndexError 或拼出空名字。
    """
    out = _FakeChatResult(
        content="",
        tool_calls=[_photo_tool_call()],
        finish_reason="tool_calls",
    )
    resp = to_llm_response(out, tools=_FakeToolSet())

    assert resp.tools_call_name == ["pc_generate_photo"], "工具名丢了 → 插件认为没调工具"
    assert resp.tools_call_args == [{"prompt": "猫"}], "参数没被 json 解析成 dict"
    assert resp.tools_call_ids == ["call_abc"], "工具调用 id 丢了 → 回传上游会错位"
    assert resp.role == "tool", "有工具调用时 role 必须是 tool（照 openai_source.py:925）"


def test_reasoning_content_survives_conversion():
    """推理内容必须保留（第 ③ 条路原来 100% 丢它）。"""
    out = _FakeChatResult(content="答案", reasoning_content="我先想想…")
    resp = to_llm_response(out, tools=None)
    assert resp.reasoning_content == "我先想想…"
    assert resp.completion_text == "答案"


def test_text_and_tool_call_can_coexist():
    """文本与工具调用**同时**存在时两者都要在（插件按 `tools_call_name` 判分支）。"""
    out = _FakeChatResult(content="我这就画", tool_calls=[_photo_tool_call()])
    resp = to_llm_response(out, tools=_FakeToolSet())
    assert resp.completion_text == "我这就画"
    assert resp.tools_call_name == ["pc_generate_photo"]


def test_no_tools_means_no_tool_call_parsing():
    """🔴 `tools is None` → **不解析** tool_calls（照真实现 `openai_source.py:881`）。

    为什么必须照抄：插件没要求工具却拿到工具调用，会去执行一个
    **它自己没申请**的动作（例如生图）—— 那是越权，不是增强。
    """
    out = _FakeChatResult(tool_calls=[_photo_tool_call()])
    resp = to_llm_response(out, tools=None)
    assert resp.tools_call_name == []
    assert resp.tools_call_args == []
    assert resp.role == "assistant", "没解析工具时 role 不该变成 tool"


def test_malformed_arguments_fall_back_to_empty_dict():
    """参数不是合法 JSON → 用 `{}` 继续（照 `openai_source.py:900-903`），**不中断**。"""
    out = _FakeChatResult(
        tool_calls=[_photo_tool_call(args="{这不是 JSON")],
    )
    resp = to_llm_response(out, tools=_FakeToolSet())
    assert resp.tools_call_name == ["pc_generate_photo"], "坏参数不该把整条工具调用丢掉"
    assert resp.tools_call_args == [{}], "坏参数应退化成空 dict"


def test_none_arguments_become_empty_dict():
    """无参工具上游可能给 `None` → `{}`（照 `openai_source.py:905-906`）。"""
    out = _FakeChatResult(
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "noop", "arguments": None}}
        ],
    )
    resp = to_llm_response(out, tools=_FakeToolSet())
    assert resp.tools_call_args == [{}]


def test_non_function_tool_call_is_skipped():
    """`type != "function"` 的调用跳过（照真实现只处理 function）。"""
    out = _FakeChatResult(
        tool_calls=[
            {"id": "c1", "type": "code_interpreter", "function": {"name": "x"}},
            _photo_tool_call(),
        ],
    )
    resp = to_llm_response(out, tools=_FakeToolSet())
    assert resp.tools_call_name == ["pc_generate_photo"]


def test_usage_is_passed_through_unchanged():
    """`usage` **原样**透传 —— 不转成 `TokenUsage`。

    为什么：插件自己有很宽的探测逻辑（`token_budget.py:257-290`
    `_usage_candidates` 会递归 `usage`/`token_usage`/`model_dump()`），
    我们转成 `TokenUsage` 反而**丢掉**它想读的原始字段
    （`completion_tokens_details.reasoning_tokens` 等）。
    """
    usage = {"prompt_tokens": 6, "completion_tokens": 12, "total_tokens": 18}
    out = _FakeChatResult(content="hi", usage=usage)
    resp = to_llm_response(out, tools=None)
    assert resp.usage == usage


def test_already_llm_response_is_returned_identically():
    """幂等：已经是 `LLMResponse` 的原样返回（避免二次包装丢字段）。"""
    from astrbot_host.compat.core.provider.entities import LLMResponse

    original = LLMResponse(completion_text="已就绪")
    assert to_llm_response(original, tools=_FakeToolSet()) is original


def test_extract_text_accepts_str_and_none():
    """纯函数契约：`None` → `""`；`str` → 原样。"""
    assert extract_text(None) == ""
    assert extract_text("直接就是字符串") == "直接就是字符串"
    assert extract_reasoning(None) == ""
    assert extract_reasoning("字符串没有推理字段") == ""


def test_nested_reasoning_field_names_are_tolerated():
    """上游/网关对推理字段叫法不一，至少认这三个常见名。

    ⚠️ 认不出来就是**静默丢推理**，所以放宽取值面比严格更安全
       （丢的是"附加信息"，不影响正文正确性）。
    """

    class _R:
        def __init__(self, **kw):
            self.content = "正文"
            for k, v in kw.items():
                setattr(self, k, v)

    assert to_llm_response(_R(reasoning_content="A"), tools=None).reasoning_content == "A"
    assert to_llm_response(_R(reasoning="B"), tools=None).reasoning_content == "B"
    assert to_llm_response(_R(thinking_content="C"), tools=None).reasoning_content == "C"


def test_parse_tool_calls_handles_json_string_call():
    """整条 tool_call 是 JSON 字符串（真实现 #1359 的 workaround）。"""
    import json as _json

    raw = _json.dumps(_photo_tool_call())
    args, names, ids = parse_tool_calls(_FakeChatResult(tool_calls=[raw]), _FakeToolSet())
    assert names == ["pc_generate_photo"]
    assert args == [{"prompt": "猫"}]
    assert ids == ["call_abc"]


# ---------------------------------------------------------------------------
# 2. 三条路各自都要通（"我改好桥但有一条没接上"必须能被抓到）
# ---------------------------------------------------------------------------


def test_provider_text_chat_preserves_tool_calls():
    """路 ① `Provider.text_chat` —— 插件按 provider 调模型时走的这条。"""
    from astrbot_host.provider import Provider

    class _LLM:
        async def generate(self, **kwargs):
            return _FakeChatResult(
                content="", tool_calls=[_photo_tool_call()], finish_reason="tool_calls"
            )

    prov = Provider(provider_id="fake", model="m", llm=_LLM())
    resp = asyncio.run(prov.text_chat(prompt="画只猫", func_tool=_FakeToolSet()))
    assert resp.tools_call_name == ["pc_generate_photo"], (
        "路①丢了工具调用 —— 插件的 function calling 在这条路上完全失效"
    )
    assert resp.tools_call_args == [{"prompt": "猫"}]
    assert resp.role == "tool"


def test_provider_text_chat_without_tools_ignores_tool_calls():
    """路 ① 也不许在没给工具集时解析（与桥同一前提）。"""
    from astrbot_host.provider import Provider

    class _LLM:
        async def generate(self, **kwargs):
            return _FakeChatResult(tool_calls=[_photo_tool_call()])

    prov = Provider(provider_id="fake", model="m", llm=_LLM())
    resp = asyncio.run(prov.text_chat(prompt="你好", func_tool=None))
    assert resp.tools_call_name == []


def test_context_as_llm_response_preserves_tool_calls():
    """路 ② `Context._as_llm_response` —— 没指定 provider 时的兜底端口。"""
    from astrbot_host.compat.core.star.context import _as_llm_response

    out = _FakeChatResult(content="", tool_calls=[_photo_tool_call()])
    resp = _as_llm_response(out, tools=_FakeToolSet())
    assert resp.tools_call_name == ["pc_generate_photo"], "路②丢了工具调用"
    assert resp.tools_call_args == [{"prompt": "猫"}]


def test_agent_runner_preserves_tool_calls_and_reasoning():
    """路 ③ `_AgentRunner.run` —— `build_main_agent` 主链（插件 9 个文件在用）。

    🔴 这条原来**两个缺口都有**：工具调用没搬，且 `_extract_reasoning`
       读 `raw.choices`（我们的 `ChatResult` 没有）→ 推理恒丢。
    """
    from astrbot_host.compat.api.provider import ProviderRequest
    from astrbot_host.compat.core.astr_main_agent import (
        MainAgentBuildConfig,
        _AgentRunner,
    )

    class _FuncTool:
        """照**真实现** `ToolSet` 的形状。

        ⚠️ 这个替身第一版写的是 `to_openai_schema()` —— 那是**旧实现的
        错误契约**（真 `FunctionTool`/`ToolSet` 都没有那个方法），
        所以它当时"通过"了，却没发现 `_tools_of` 恒返回 None 的真 bug。
        ✅ 现在照真实现用 `openai_schema()`，判据才真的盯着它。
        """

        def openai_schema(self, omit_empty_parameter_field: bool = False):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "pc_generate_photo",
                        "description": "画图",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]

        def get_all(self):
            return []

    class _LLM:
        async def generate(self, **kwargs):
            assert kwargs.get("tools") is not None, "路③必须把工具集交给上游"
            return _FakeChatResult(
                content="",
                tool_calls=[_photo_tool_call()],
                reasoning_content="决定画猫",
            )

    req = ProviderRequest(prompt="画只猫")
    req.func_tool = _FuncTool()
    runner = _AgentRunner(
        llm=_LLM(), req=req, config=MainAgentBuildConfig()
    )
    resp = asyncio.run(runner.run())

    assert resp.tools_call_name == ["pc_generate_photo"], "路③丢了工具调用"
    assert resp.reasoning_content == "决定画猫", (
        "路③推理内容丢失（原实现读 raw.choices，而 ChatResult 没有该属性）"
    )
    assert resp.role == "tool"


def test_agent_runner_role_is_assistant_without_tool_calls():
    """路 ③ 无工具调用时 `role` 仍是 assistant（别把普通回复标成 tool）。"""
    from astrbot_host.compat.api.provider import ProviderRequest
    from astrbot_host.compat.core.astr_main_agent import (
        MainAgentBuildConfig,
        _AgentRunner,
    )

    class _LLM:
        async def generate(self, **kwargs):
            return _FakeChatResult(content="你好呀")

    runner = _AgentRunner(
        llm=_LLM(), req=ProviderRequest(prompt="hi"), config=MainAgentBuildConfig()
    )
    resp = asyncio.run(runner.run())
    assert resp.role == "assistant"
    assert resp.completion_text == "你好呀"


# ---------------------------------------------------------------------------
# 3. 唯一入口（源码级判据 —— 防止第 4 条路又自己拼一个）
# ---------------------------------------------------------------------------

#: 允许构造 `LLMResponse` 的文件（白名单）。
#:
#: * `response_bridge.py` —— **唯一**的业务构造处；
#: * `provider/entities.py` / `api/provider/__init__.py` —— 类定义与 `__repr__`；
#: * `astr_main_agent.py` —— 只在**流式分块**等非本次范围处使用（见下方排除理由）。
_ALLOWED_LLM_RESPONSE_CTORS = {
    "astrbot_host/response_bridge.py",
    "astrbot_host/compat/core/provider/entities.py",
    "astrbot_host/compat/api/provider/__init__.py",
}


def test_no_second_handbuilt_llm_response():
    """🔴 **唯一入口判据**：除白名单外，不许再有 `LLMResponse(...)` 构造。

    为什么这条最值钱：这次的真 bug 就是"三条路各拼了一份，其中两份悄悄少字段"。
    靠"我这次都改对了"守不住 —— 下次加第四条路（例如流式、或新的 agent 入口）
    会**再犯一次同样的错且很难发现**。把"只许一个入口"变成判据才守得住。

    ⚠️ 扫描是**文本级**的（`re` 找 `LLMResponse(`），可能误报注释/字符串；
       所以报错信息里列出具体行，便于人工确认后调整白名单或去掉重复实现。
    """
    import re

    pattern = re.compile(r"\bLLMResponse\s*\(")
    offenders: list[str] = []
    for path in sorted((HOST_DIR / "astrbot_host").rglob("*.py")):
        rel = path.relative_to(HOST_DIR).as_posix()
        if rel in _ALLOWED_LLM_RESPONSE_CTORS:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            # 跳过注释（本项目的注释里会引用这行代码，是**故意**的文档）
            if stripped.startswith("#"):
                continue
            # 跳过纯字符串/文档里的示例（启发式：行内没有赋值或 return）
            if "LLMResponse(" in line and pattern.search(line):
                if stripped.startswith(('"', "'", "*", "-")):
                    continue
                offenders.append(f"{rel}:{lineno}: {stripped}")

    assert not offenders, (
        "发现 `response_bridge.to_llm_response` 之外**手工构造** LLMResponse 的地方。\n"
        "这会造成'某些字段在某些路径上悄悄丢失'（本次真 bug 的根因）。\n"
        "→ 请改成调用 `response_bridge.to_llm_response(...)`。\n"
        "若确属必要（如纯类型定义），把它加进 `_ALLOWED_LLM_RESPONSE_CTORS` "
        "并写明理由。\n\n匹配到：\n" + "\n".join(offenders)
    )


def test_response_bridge_is_the_single_source_for_python_consumers():
    """三条路**都**必须 import 桥（源码级，防"接了两条漏一条"）。"""
    expected = {
        "astrbot_host/provider.py",
        "astrbot_host/compat/core/star/context.py",
        "astrbot_host/compat/core/astr_main_agent.py",
    }
    missing: list[str] = []
    for rel in sorted(expected):
        text = (HOST_DIR / rel).read_text(encoding="utf-8")
        if "response_bridge" not in text:
            missing.append(rel)
    assert not missing, (
        "这些路径没有走唯一转换入口 `response_bridge`：\n" + "\n".join(missing)
    )


# ---------------------------------------------------------------------------
# 4. 顺带修好的：`MainAgentBuildConfig` 根本构造不出来
# ---------------------------------------------------------------------------


def test_main_agent_build_config_is_constructible():
    """🔴 `MainAgentBuildConfig(...)` 必须能构造 —— 这是**第二个真 bug**。

    原来它写成 `@dataclass(slots=True)` **外加**一个手写 `__init__`：
    手写的那个把 dataclass 生成的**整个覆盖**掉 →
      * 所有字段默认值都没被应用；
      * `self.extra[k] = v` 读一个从未初始化的属性 → `AttributeError`。
    ⇒ **任何调用都抛**，插件那两条路（`private_image.py:6412`
      私有图片回复 / `proactive_message.py:5093` 主动消息）在此直接断掉。

    ⚠️ 判据用**插件真实的传参形状**（照抄那两行），
       而不是 `MainAgentBuildConfig()` —— 只测无参构造会漏掉这条。
    """
    from astrbot_host.compat.core.astr_main_agent import MainAgentBuildConfig

    # ① 插件真实传法（三个关键字，逐个照抄）
    cfg = MainAgentBuildConfig(
        tool_call_timeout=120,
        llm_safety_mode=False,
        streaming_response=False,
    )
    assert cfg.tool_call_timeout == 120
    assert cfg.llm_safety_mode is False
    assert cfg.streaming_response is False

    # ② 默认值必须真的生效（手写 __init__ 时它们全是"未设置"）
    default = MainAgentBuildConfig()
    assert default.tool_call_timeout == 120
    assert default.tool_schema_mode == "full"
    assert default.streaming_response is True
    assert default.max_context_length == 50
    assert default.dequeue_context_length == 10


def test_main_agent_build_config_rejects_unknown_fields_like_real_impl():
    """照真实现：真 `MainAgentBuildConfig` 是纯 dataclass，**不认未知字段**。

    ⚠️ 我们以前自造了一个 `**kwargs` + `extra` 兜底（既没生效过，
       又与真实现行为不一致）→ 已删。这条判据守住"别再加回来"：
       如果哪天有人为了"兼容"把它改成静默收下未知键，这里会失败 ——
       那个改动会让**插件传错参数时不报错**（静默降级）。
    """
    from astrbot_host.compat.core.astr_main_agent import MainAgentBuildConfig

    with pytest.raises(TypeError):
        MainAgentBuildConfig(某个我们没实现的字段=1)


# ---------------------------------------------------------------------------
# 5. `astrbot.core.agent.tool`（`FunctionTool` / `ToolSet`）—— 缺了就静默失败
# ---------------------------------------------------------------------------


def test_agent_tool_module_resolves_through_shadow():
    """🔴 `astrbot.core.agent.tool` 必须能 import。

    **真 bug 实测**（对真服务发请求拍到的响应）::

        {"error": "No module named 'astrbot.core.agent.tool'",
         "error_stage": "tool_decision", "tool_status": "model_error"}

    插件把它包在 `except Exception` 里 → 只记一行 info 日志，
    面板显示"模型判断失败"，**完全指不到根因**（看着像模型/网络问题）。
    调用方：`page_api.py:4199` / `:4217`（参考图试跑的 Function Calling）。

    ⚠️ **判据写法踩过一次**（注入验证第 7 组抓到）：第一版只做
       `install_shadow_package()` + `from astrbot.core.agent.tool import ...`
       —— 而 `shadow` 装过之后模块已在 `sys.modules` 里，
       **把注册表里的 `"core.agent.tool"` 删掉它照样绿**。
       ✅ 现在**直接断言注册表里有这一项**（那才是被测对象）+
       独立进程里真 import（验证文件真的存在且可导入）。
    """
    import subprocess

    from astrbot_host import shadow

    assert "core.agent.tool" in shadow.SUBMODULES, (
        "`core.agent.tool` 没注册进 shadow 模块表 → 插件 "
        "`from astrbot.core.agent.tool import FunctionTool, ToolSet` 会 "
        "ModuleNotFoundError，而它只记一行 info 日志（面板显示『模型判断失败』）"
    )
    # 独立进程里真 import（`sys.modules` 缓存挡不住这一层）
    code = (
        "import sys; sys.path.insert(0, 'host');"
        "from astrbot_host.shadow import install_shadow_package;"
        "install_shadow_package();"
        "from astrbot.core.agent.tool import FunctionTool, ToolSet;"
        "print('OK', FunctionTool.__name__, ToolSet.__name__)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(HOST_DIR.parent),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"独立进程 import 失败：\n{proc.stdout}\n{proc.stderr}"
    )
    assert "OK FunctionTool ToolSet" in proc.stdout


def test_toolset_openai_schema_matches_real_contract():
    """`openai_schema()` 的输出必须逐字照真实现（`tool.py:203-217`）。

    🔴 这个格式直接进上游请求体的 `tools` 字段 —— 差一个键就是
       上游 400 或模型收不到工具（**功能静默失效**）。
    """
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot.core.agent.tool import FunctionTool, ToolSet

    tool = FunctionTool(
        name="pc_generate_photo",
        description="画图",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}},
        handler=None,
    )
    schema = ToolSet([tool]).openai_schema()
    assert schema == [
        {
            "type": "function",
            "function": {
                "name": "pc_generate_photo",
                "description": "画图",
                "parameters": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                },
            },
        }
    ]


def test_toolset_omits_empty_description_and_none_parameters():
    """空 `description` / `None` parameters **整个键都不出现**（照真实现）。

    ⚠️ 写成空串 `""` 或 `parameters: None` 与真实现不同 ——
       有的上游会把 `parameters: null` 判 400。
    """
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot.core.agent.tool import FunctionTool, ToolSet

    schema = ToolSet([FunctionTool(name="t", description="", parameters=None)]).openai_schema()
    fn = schema[0]["function"]
    assert "description" not in fn, "空 description 不该出现该键"
    assert "parameters" not in fn, "parameters=None 不该出现该键"


def test_toolset_none_handler_raises_like_real_impl():
    """无 handler 时 `call()` **如实抛**（照真实现 `:69-73`）。

    ⚠️ 插件的试跑工具**故意**传 `handler=None`（只要模型"说出"要调什么，
       不真执行）—— 所以这条判据守的是"别把无 handler 做成静默空成功"。
    """
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot.core.agent.tool import FunctionTool

    with pytest.raises(NotImplementedError):
        asyncio.run(FunctionTool(name="t", handler=None).call())


# ---------------------------------------------------------------------------
# 6. 工具集**格式转换**（`normalize_tools`）—— 原样透传会炸，且错误离根因三层
# ---------------------------------------------------------------------------


def test_normalize_tools_converts_toolset_object():
    """⭐ `ToolSet` **对象**必须转成 OpenAI schema 列表。

    **真 bug 实测**（服务日志原文）::

        TypeError: Object of type ToolSet is not JSON serializable
        During handling of the above exception, another exception occurred:
        ...
        ValueError: 上游 base_url 为空：无法拼出请求地址

    第二段错**完全是误导** —— 根因是"工具集没转格式"，
    现场却看到"base_url 为空"。
    """
    from astrbot_host.response_bridge import normalize_tools
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot.core.agent.tool import FunctionTool, ToolSet

    tool = FunctionTool(
        name="pc_generate_photo",
        description="画图",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}},
        handler=None,
    )
    out = normalize_tools(ToolSet([tool]))
    assert isinstance(out, list) and out, "没转出来 → 上游会收到一个不能序列化的对象"
    assert out[0]["function"]["name"] == "pc_generate_photo"
    # 关键：能真的 json 序列化（这才是"能不能发给上游"的判据）
    import json as _json

    _json.dumps({"tools": out})


def test_normalize_tools_accepts_list_of_dicts_unchanged():
    """已经是 OpenAI 格式的 list[dict] → **原样**返回（别二次包装）。"""
    from astrbot_host.response_bridge import normalize_tools

    already = [{"type": "function", "function": {"name": "x"}}]
    assert normalize_tools(already) == already


def test_normalize_tools_accepts_list_of_function_tools():
    """`list[FunctionTool]` 也要能转（插件有的地方直接给列表）。"""
    from astrbot_host.response_bridge import normalize_tools
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot.core.agent.tool import FunctionTool

    tool = FunctionTool(name="t", description="d", parameters={"type": "object", "properties": {}})
    out = normalize_tools([tool])
    assert out and out[0]["function"]["name"] == "t"


def test_normalize_tools_returns_none_for_empty():
    """空工具集 → `None`（**不传** `tools` 键，而不是传空数组）。

    ⚠️ 传 `tools: []` 有的上游会判 400。
    """
    from astrbot_host.response_bridge import normalize_tools

    assert normalize_tools(None) is None
    assert normalize_tools([]) is None


def test_provider_text_chat_converts_tools_before_upstream():
    """路① `Provider.text_chat` 必须**转完格式**再交给上游（不是原样透传）。"""
    from astrbot_host.provider import Provider
    from astrbot_host.shadow import install_shadow_package

    install_shadow_package()
    from astrbot.core.agent.tool import FunctionTool, ToolSet

    seen: dict[str, object] = {}

    class _LLM:
        async def generate(self, **kwargs):
            seen.update(kwargs)
            return _FakeChatResult(content="ok")

    tool = FunctionTool(name="x", description="d", parameters={"type": "object", "properties": {}})
    prov = Provider(provider_id="p", model="m", llm=_LLM())
    asyncio.run(prov.text_chat(prompt="hi", func_tool=ToolSet([tool])))

    tools = seen.get("tools")
    assert isinstance(tools, list), (
        "交给上游的 tools 不是 list（原样透传 ToolSet 对象 → 序列化 TypeError）"
    )
    import json as _json

    _json.dumps({"tools": tools})  # 能序列化才算真的能发出去


def test_signature_mismatch_helper_does_not_swallow_serialization_errors():
    """🔴 `_looks_like_signature_mismatch` 必须**只认签名问题**。

    为什么关键：`UpstreamLLMPort.generate` 的兜底重试原来无条件接住
    任何 `TypeError` → 把序列化错误也当成签名问题 →
    用不带 `base_url` 的签名重试 → 报「上游 base_url 为空」（**误导**）。
    """
    from emotion_app.web.provider_bridge import _looks_like_signature_mismatch

    kwargs = {"messages": [], "model": "m", "base_url": "http://x", "tools": []}
    # ① 序列化错误 → 不是签名问题（必须继续抛，让真因暴露）
    assert not _looks_like_signature_mismatch(
        TypeError("Object of type ToolSet is not JSON serializable"), kwargs
    )
    # ② 真正的签名问题 → 认
    assert _looks_like_signature_mismatch(
        TypeError("complete() got an unexpected keyword argument 'base_url'"), kwargs
    )
    # ③ 别的 TypeError（例如 None 下标）→ 不是签名问题
    assert not _looks_like_signature_mismatch(TypeError("'NoneType' is not subscriptable"), kwargs)


def test_llm_port_propagates_serialization_typeerror_instead_of_retrying():
    """🔴🔴 **接线判据**：非签名类 `TypeError` 必须**继续抛**，不许被兜底吞掉。

    ⚠️ 这条是补的 —— 我第一版只测了 `_looks_like_signature_mismatch` **函数本身**，
       结果把接线改回"无条件吞"（注入验证第 6 组）**31 条判据全绿**。
       **函数对 ≠ 接线对**（本项目老坑：「模块能工作 ≠ 被接上了」）。
       → 所以必须在**真实调用路径**上测：

       真 bug 形态：上游 `json` 序列化 `ToolSet` 抛
       `TypeError: Object of type ToolSet is not JSON serializable`
       → 被"签名不匹配"兜底接住 → 用**不带 base_url** 的签名重试
       → 最后报「上游 base_url 为空」（**离根因三层**）。
    """
    from emotion_app.web.provider_bridge import UpstreamLLMPort

    calls: list[dict] = []

    class _Backend:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            # 第一次（带完整参数）抛序列化错误；重试路径**不该发生**
            raise TypeError("Object of type ToolSet is not JSON serializable")

    port = UpstreamLLMPort(
        backend=_Backend(), model="m", base_url="http://upstream/v1", api_key="k"
    )
    with pytest.raises(TypeError) as e:
        asyncio.run(port.generate(prompt="hi"))
    assert "not JSON serializable" in str(e.value), "真因必须原样暴露给调用方"
    assert len(calls) == 1, (
        f"发生了兜底重试（调了 {len(calls)} 次）→ 真因被埋，"
        "现场会看到误导性的『base_url 为空』"
    )


def test_llm_port_retries_on_genuine_signature_mismatch():
    """反向判据：**真正的**签名不匹配仍要重试（别把兜底删掉）。

    ⚠️ 只测"吞错被抓"会让下一个人把兜底整个删掉 —— 那会打断
       "后端只认 messages/model"的兼容路径。两个方向都要有判据。
    """
    from emotion_app.web.provider_bridge import UpstreamLLMPort

    calls: list[dict] = []

    class _Backend:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TypeError(
                    "complete() got an unexpected keyword argument 'base_url'"
                )
            return _FakeChatResult(content="兜底成功")

    port = UpstreamLLMPort(
        backend=_Backend(), model="m", base_url="http://upstream/v1", api_key="k"
    )
    out = asyncio.run(port.generate(prompt="hi"))
    assert len(calls) == 2, "真正的签名不匹配应当重试一次"
    assert out.content == "兜底成功"
    # 第二次必须用最简签名（这正是兜底的意义）
    assert "base_url" not in calls[1]
