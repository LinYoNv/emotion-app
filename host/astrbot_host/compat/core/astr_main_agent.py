# -*- coding: utf-8 -*-
"""`astrbot.core.astr_main_agent` 兼容层 —— 主对话代理构建。

⭐⭐ **照真 AstrBot `core/astr_main_agent.py`（1754 行）的【对外契约】实现。**

🔴 **为什么这个模块决定陪伴能不能真的"说话"**：
   实测陪伴在**四条关键路径**上调用它（各 16-17 处）：
   `proactive_message.py` / `private_image.py` / `user_memory.py` / `group_observation.py`
   —— 即**主动消息、生图、记忆、群观察**都要经它发起 LLM 请求。

   ⚠️ 所以**不能做成占位符**（抛 NotImplementedError 会让陪伴的核心功能全废）。
   正确做法：实现一个**行为等价的精简版** —— 装配 `ProviderRequest`，
   调宿主的 `LLMPort`，返回 `MainAgentBuildResult`。

⚠️ **与真实现的能力差异（如实记录，不假装等价）**：
   真实现构建的是完整 **AgentRunner**（含工具循环、上下文压缩、安全模式、
   checkpoint、多 provider 选择…，1754 行的量级）。
   我们实现的是**单轮 LLM 调用 + 工具循环的最小版**：
   * ✅ `req.system_prompt` / `req.contexts` / `req.prompt` 会正确送达模型
   * ✅ 返回 `agent_runner`，其 `run()` 产出文本（插件靠它取回复）
   * ⚠️ 不做上下文压缩、不做 provider 轮询、不做 checkpoint
   * ⚠️ 工具循环为**单轮**（宿主侧 `max_calls_per_turn=1` 的既有策略）
   → 差异**显式记录在此**，并在 `capabilities()` 里可查。
"""
from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from astrbot_host.compat.api.provider import LLMResponse, ProviderRequest
from astrbot_host.compat.core.provider.entities import ToolCallsResult
from astrbot_host.response_bridge import to_llm_response

logger = logging.getLogger("astrbot")

#: 真实现里的错误键名（插件会读它判断是否出错）
LLM_ERROR_MESSAGE_EXTRA_KEY = "llm_error_message"


@dataclass(slots=True)
class MainAgentBuildConfig:
    """主代理构建配置（字段照真实现，**只实现被用到的**）。

    真实现有 30+ 字段；这里保留插件实测会传的那几个，
    其余以 `**kwargs` 兜住并**记录**（便于发现"插件还传了什么"）。

    ## 🔴 为什么不用 `@dataclass` 自动生成的 `__init__`（2026-09-21 修的真 bug）

    原来这里写了个自定义 `__init__`，而 `@dataclass` **同时也生成了一个** ——
    手写的那个把它**整个覆盖**掉，于是：

      * 所有字段的**默认值都没被应用**（`tool_call_timeout` / `tool_schema_mode` /
        `streaming_response`… 全是"未设置"）；
      * 更致命的是 `self.extra[k] = v` 与 `if self.extra` 直接读一个
        **从未初始化的属性** → `AttributeError`。

    ⇒ **`MainAgentBuildConfig(...)` 在任何调用下都抛异常**
      （实测：`MainAgentBuildConfig()` 与插件真实的
      `MainAgentBuildConfig(tool_call_timeout=120, llm_safety_mode=False,
      streaming_response=False)` **都抛**）。

    ⇒ 插件那两条路 —— **私有图片回复**（`private_image.py:6412`）与
      **主动消息**（`proactive_message.py:5093`）—— 会在此处**直接断掉**。

    ✅ 修法**照真实现**：真 `MainAgentBuildConfig` 就是一个
      `@dataclass(slots=True)` **且没有自定义 `__init__`**
      （`astrbot/core/astr_main_agent.py:159`，实测确认）→
      我们删掉那个手写 `__init__`，让 dataclass 自己生成。

    ⚠️ **`extra` 不再是 `**kwargs` 兜底**：真实现的 dataclass 只认已声明字段，
      传未知键会 `TypeError`。我们以前那个 `**kwargs` + `extra` 收集是**自造的**
      （本意是"发现插件还传了什么"），但它既没生效过（见上），
      又与真实现行为不一致 → **删掉它，行为照真实现**。
      想发现"插件传了什么"，去读插件源码或在调用点记日志，别改契约。
    """

    tool_call_timeout: int = 120
    """工具调用超时（秒）"""
    tool_schema_mode: str = "full"
    provider_wake_prefix: str = ""
    streaming_response: bool = True
    llm_safety_mode: bool = True
    sanitize_context_by_modalities: bool = False
    max_context_length: int = 50
    dequeue_context_length: int = 10


class _AgentRunner:
    """最小 agent runner（真实现 `ToolLoopAgentRunner` 的等价物）。

    插件通过它拿到模型输出。真实现的接口面更宽，这里只实现
    **被实际调用**的部分，其余显式报错。

    ## 🔴🔴 2026-09-22：补上**工具循环**（原先是"单轮"，等于工具永不执行）

    真实现是 `astrbot.core.agent.runners.tool_loop_agent_runner.ToolLoopAgentRunner`
    （本机可读：`/root/.local/share/uv/tools/astrbot/.../astrbot/` v4.27.5）。
    插件对 runner 的调用面**实测只有这些**（grep `runner.` / `agent_runner.`）：

    | 方法 | 插件调用点 | 缺了会怎样（实测） |
    |---|---|---|
    | `step_until_done(max_step)` | `private_image.py:6555`、`proactive_message.py:4665` | 两处都有 `hasattr` 守卫 → **静默跳过整个工具循环** |
    | `get_final_llm_resp()` | `private_image.py:6599`、`proactive_message.py:5188` | **无守卫** → `AttributeError` 直接抛出 |

    ⚠️ 我们原来**两个都没有** → 「工具能被模型说出来」之后就断了：
    工具从不执行、结果从不回填、主动消息/私有图片两条链在取最终回复时崩。

    ## 语义（照真实现，别自己发明）

    * `step()`：发一次 LLM 请求（`tool_loop_agent_runner.py:787`）。
      无 `tools_call_name` → `_complete_with_assistant_response` → 状态置 `DONE`；
      有 → 执行每个工具、把结果作为 `role="tool"` 消息并回上下文（`:928-1058`），
      状态**保持 RUNNING** → `step_until_done` 会再跑一轮。
    * `done()`：`self._state in (DONE, ERROR)`（`:1467-1469`）。
    * `step_until_done(max_step)`：`while not done() and count < max_step`；
      **达到上限**时真实现会**拔掉全部工具**并追加 `MAX_STEPS_REACHED_PROMPT`
      再跑最后一步（`:1060-1087`）—— 照抄，否则模型会无限要求调工具。
    """

    #: 达到步数上限时注入的提示词（照真实现 `:1078` 的 `MAX_STEPS_REACHED_PROMPT`）
    MAX_STEPS_REACHED_PROMPT = (
        "You have reached the maximum number of steps. "
        "Please provide your final response based on the information available."
    )

    def __init__(
        self,
        *,
        llm: Any,
        req: ProviderRequest,
        config: MainAgentBuildConfig,
        event: Any = None,
    ) -> None:
        self._llm = llm
        self._req = req
        self._config = config
        #: 触发本轮的事件 —— 工具 handler 的第一个位置参数就是它
        #: （真实现 `astr_agent_tool_exec.py:738` `handler(event, ...)`）。
        self._event = event
        #: 最近一次响应（插件可能读）
        self.last_response: LLMResponse | None = None
        #: 最终响应（`get_final_llm_resp()` 返回它；照真实现 `final_llm_resp`）
        self.final_llm_resp: LLMResponse | None = None
        #: 状态机（照真实现 `AgentState`，只用得到的三个值）
        self._state: str = "IDLE"
        #: 多轮消息（工具循环要**跨轮累积**，所以不能每轮重建）
        self._messages: list[dict[str, Any]] = []
        self._messages_built = False

    # ---- 内部：消息累积 ----

    def _ensure_messages(self) -> list[dict[str, Any]]:
        """首次调用时按 `req` 装配初始消息（之后跨轮累积）。"""
        if self._messages_built:
            return self._messages
        if self._req.system_prompt:
            self._messages.append(
                {"role": "system", "content": self._req.system_prompt}
            )
        for c in self._req.contexts or []:
            if isinstance(c, dict):
                self._messages.append(c)
        if self._req.prompt:
            self._messages.append({"role": "user", "content": self._req.prompt})
        self._messages_built = True
        return self._messages

    def _tools(self) -> list[dict[str, Any]] | None:
        """本次要交给上游的工具（`None` = 没有）。"""
        return _tools_of(self._req)

    async def _call_llm(self) -> Any:
        tools = self._tools()
        return await self._llm.generate(
            messages=self._ensure_messages(),
            system_prompt=self._req.system_prompt or None,
            model=self._req.model,
            tools=tools,
        )

    # ---- 照真实现的四个方法 ----

    def done(self) -> bool:
        """是否已完成（照真实现 `:1467-1469`）。"""
        return self._state in ("DONE", "ERROR")

    def get_final_llm_resp(self) -> LLMResponse | None:
        """取最终响应（照真实现 `:1480-1481`）。

        🔴 **插件无守卫地调用它**（`private_image.py:6599`、`proactive_message.py:5188`）
        —— 没这个方法就是 `AttributeError`。
        """
        return self.final_llm_resp

    async def step(self) -> AsyncGenerator[LLMResponse, None]:
        """跑一步（一次 LLM 调用 + 必要时执行工具）。

        产出与真实现同族的对象：`LLMResponse`。
        ⚠️ 真实现产出的是 `AgentResponse`（带 `type` 的包装）；
           插件那两处只用 `step_until_done` 的**副作用**（`async for _ in ...: pass`），
           不读产出的字段 —— 所以产出 `LLMResponse` 是**如实够用**的，
           且比伪造一个空壳 `AgentResponse` 更不容易误导。
        """
        if self._state == "IDLE":
            self._state = "RUNNING"
        raw = await self._call_llm()
        resp = to_llm_response(raw, tools=self._req.func_tool)
        resp.raw_completion = raw
        self.last_response = resp

        if not resp.tools_call_name:
            # 没有工具调用 → 这就是最终回复（照真实现 `_complete_with_assistant_response`）
            if not resp.role or resp.role == "tool":
                resp.role = "assistant"
            self.final_llm_resp = resp
            self._state = "DONE"
            yield resp
            return

        # ---- 有工具调用：执行并把结果回填上下文 ----
        await self._execute_tools(resp)
        # ⚠️ **不置 DONE**：照真实现保持 RUNNING，让 `step_until_done` 再跑一轮，
        #    把工具结果喂回模型拿最终回复。
        self.final_llm_resp = resp
        yield resp

    async def step_until_done(
        self, max_step: int
    ) -> AsyncGenerator[LLMResponse, None]:
        """跑到完成或达上限（照真实现 `:1060-1087`）。"""
        step_count = 0
        steps = max(1, int(max_step or 1))
        while not self.done() and step_count < steps:
            step_count += 1
            async for resp in self.step():
                yield resp

        if not self.done():
            # 照真实现：达上限 → **拔掉所有工具** + 注入提示词 + 再跑最后一步
            logger.warning(
                "Agent reached max steps (%s), forcing a final response.", steps
            )
            self._req.func_tool = None
            self._messages.append(
                {"role": "user", "content": self.MAX_STEPS_REACHED_PROMPT}
            )
            async for resp in self.step():
                yield resp

    # ---- 工具执行（照真实现 `_handle_function_tools` :1089-1200） ----

    async def _execute_tools(self, resp: LLMResponse) -> None:
        """执行 `resp` 里的每个工具调用，把结果并回消息上下文。

        🔴 **这是本项目最缺的那一环**：原来函数在这里就返回了
        （`run()` 直接 `return`），工具**从未被执行**。

        照真实现的四个要点：
          ① 失败**不抛**，而是把错误文本当**工具结果**回填
             （`:1156-1159` `"error: Tool X not found. Available tools are: ..."`）
             —— 让模型自己纠正，而不是整轮崩掉；
          ② 参数按 schema `properties` **过滤**（`:1169-1176`），
             非期望参数记 WARNING（不静默丢）；
          ③ 无 handler → 用全部参数（MCP 工具那种，`:1188-1190`）；
          ④ 结果作为 `role="tool"` 消息（带 `tool_call_id`）**累积**进上下文。
        """
        tool_set = self._req.func_tool
        #: 助手那条带 tool_calls 的消息（OpenAI 协议要求 tool 消息前必须有它）
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": resp.completion_text or None,
            "tool_calls": [],
        }
        tool_results: list[dict[str, Any]] = []

        for name, args, call_id in zip(
            resp.tools_call_name,
            resp.tools_call_args,
            resp.tools_call_ids,
        ):
            func_tool = None
            if tool_set is not None and hasattr(tool_set, "get_tool"):
                func_tool = tool_set.get_tool(name)
            available = (
                tool_set.names()
                if tool_set is not None and hasattr(tool_set, "names")
                else []
            )
            assistant_msg["tool_calls"].append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": _json_args(args),
                    },
                }
            )

            if func_tool is None:
                logger.warning(
                    "未找到指定的工具: %s（可用：%s），已把错误作为工具结果回填",
                    name,
                    ", ".join(available) or "（无）",
                )
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": (
                            f"error: Tool {name} not found. "
                            f"Available tools are: {', '.join(available)}"
                        ),
                    }
                )
                continue

            handler = getattr(func_tool, "handler", None)
            params = getattr(func_tool, "parameters", None) or {}
            tool_args = args if isinstance(args, dict) else {}
            if params.get("properties"):
                expected = set(params["properties"].keys())
                valid_args = {k: v for k, v in tool_args.items() if k in expected}
                ignored = set(tool_args) - set(valid_args)
                if ignored:
                    logger.warning("工具 %s 忽略非期望参数: %s", name, ignored)
            else:
                valid_args = tool_args

            if handler is None:
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": (
                            f"error: Tool {name} has no handler and cannot be "
                            "executed by this host."
                        ),
                    }
                )
                continue

            logger.info("使用工具：%s，参数：%s", name, valid_args)
            try:
                result = await _invoke_tool_handler(
                    handler, valid_args, event=self._event
                )
                content = result if isinstance(result, str) else str(result)
            except Exception as exc:  # noqa: BLE001
                # ⚠️ **不吞**：如实记 ERROR **并**把错误回填给模型（两条都要）。
                #    照真实现 `astr_agent_tool_exec.py:742+` 的做法：
                #    工具异常变成工具结果，让模型能纠正而不是整轮崩。
                logger.error("工具 %s 执行失败：%s", name, exc, exc_info=True)
                content = f"error: Tool {name} execution failed: {exc}"
            tool_results.append(
                {"role": "tool", "tool_call_id": call_id, "content": content}
            )

        self._messages.append(assistant_msg)
        self._messages.extend(tool_results)
        # 照真实现 `:1058` 把结果也挂到 req 上（真实现的 provider 从这儿取）
        try:
            self._req.append_tool_calls_result(
                ToolCallsResult(
                    tool_calls_info=assistant_msg,
                    tool_calls_result=tool_results,
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("append_tool_calls_result 不可用（不影响本循环）", exc_info=True)

    async def run(self) -> LLMResponse:
        """执行一次生成（返回 `LLMResponse`）。

        ⚠️ **保留原语义（单轮、不执行工具）**：插件里有 `run()` 的调用点，
           它们要的是"这一次模型说了什么"，不是"跑完工具循环"。
           要工具循环请用 `step_until_done()`（照真实现）。
        """
        self._ensure_messages()
        raw = await self._call_llm()
        # 🔴🔴 **走唯一转换入口**（2026-09-21 修的真 bug）。
        #
        #    这一条路原来自己拼 `LLMResponse(role="assistant",
        #    completion_text=..., reasoning_content=_extract_reasoning(raw))`，
        #    有两个缺口：
        #      ① `tool_calls` **完全没搬** → 插件认为"模型没调工具"；
        #      ② `_extract_reasoning(raw)` 读的是 `raw.choices[0].message...`，
        #         而我们的返回值是领域层 `ChatResult`（**没有 `.choices`**）
        #         → 恒返回 None。即**推理内容在这条路上 100% 丢失**。
        #
        #    ⚠️ `build_main_agent` 是插件**9 个文件**都在用的主链
        #       （`main.py:63`、`event_dispatch.py:53`、`private_image.py:29`…），
        #       所以这条路上的静默丢失影响面最大。
        resp = to_llm_response(raw, tools=self._req.func_tool)
        # `role` 只在没有工具调用时才该是 assistant（有工具调用时
        # `to_llm_response` 已按真实现置 "tool"，**不要覆盖它**）。
        if not resp.tools_call_name:
            resp.role = "assistant"
        resp.raw_completion = raw
        self.last_response = resp
        self.final_llm_resp = resp
        self._state = "DONE"
        return self.last_response

    async def run_stream(self) -> AsyncGenerator[LLMResponse, None]:
        """流式生成（真实现支持；我们按整流分块产出）。

        ⚠️ 宿主 `LLMPort` 若支持流式，另接；当前实现**整流后单块产出** ——
           语义正确（调用方拿到的内容一致），只是不增量。
           这是**如实降级**，且已在 `capabilities()` 里标明。
        """
        yield await self.run()

    def __repr__(self) -> str:
        return f"<_AgentRunner config={self._config.tool_schema_mode}>"


@dataclass
class MainAgentBuildResult:
    """构建结果（字段照真实现）。"""

    agent_runner: _AgentRunner
    provider_request: ProviderRequest
    provider: Any
    reset_coro: Any = None


def _json_args(args: Any) -> str:
    """把工具参数转成 OpenAI `tool_calls[].function.arguments`（**必须是 JSON 字符串**）。

    ⚠️ OpenAI 协议里 `arguments` 是**字符串**不是对象；回填上下文时如果给 dict，
       部分上游会 400（"arguments must be a string"）。真实现走
       `AssistantMessageSegment.model_dump()`，pydantic 会保证是字符串。
    """
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


async def _invoke_tool_handler(
    handler: Any, kwargs: dict[str, Any], *, event: Any = None
) -> Any:
    """调用工具 handler（照真实现 `astr_agent_tool_exec.py:738` 的形状）。

    🔴 **真实现的调用形状是 `handler(event, *args, **kwargs)`** —— 第一个位置参数
    是 **event**。而 `@filter.llm_tool` 装饰的是插件的**方法**，装饰器把
    `md.handler` 存进工具表时**不含 self**（`register_llm_tool` 的 `awaitable`
    就是裸函数）；真实现靠 `star_manager.py:1273-1299` 在**加载后**把它替换成
    `functools.partial(raw_handler, 插件实例)`。

    所以我们这里**只管调**：`partial` 已经把 self 填好了，event 作为第一个位置参数。

    ⚠️ handler 可能是**同步**函数（插件的工具大多是 `async def`，但不保证）——
       同步的直接返回，别 `await` 一个非 awaitable（那会 TypeError）。
    """
    args = dict(kwargs or {})
    if event is None:
        result = handler(**args)
    else:
        try:
            result = handler(event, **args)
        except TypeError as exc:
            # ⚠️ **必须区分"签名不匹配"和"handler 内部抛的 TypeError"**
            #    （本项目踩过：宽 `except TypeError` 把真因盖掉）。
            #    只有错误信息里确实出现我们传的参数名/参数个数才算签名问题。
            if not _looks_like_signature_mismatch(exc, args):
                raise
            logger.debug(
                "工具 handler 不接受 event 位置参数，改按关键字调用：%s", exc
            )
            result = handler(**args)
    if inspect.isawaitable(result):
        return await result
    return result


def _looks_like_signature_mismatch(exc: TypeError, kwargs: dict[str, Any]) -> bool:
    """这个 `TypeError` 真是"参数不匹配"吗（而不是 handler 内部的错）。

    🔴 判据（本项目铁律：**宽 `except` 兜底必须校验"这真是我认识的那类错"**）：
      * `unexpected keyword argument '<k>'` 且 `<k>` ∈ 我们传的键；
      * `missing ... required positional argument` / `positional argument`；
      * `takes N positional arguments but M were given`。
    其余一律**不认**（原样抛出去，别把真因换成一个看起来合理的假错）。
    """
    msg = str(exc)
    if "unexpected keyword argument" in msg:
        return any(f"'{k}'" in msg for k in kwargs)
    return (
        "required positional argument" in msg
        or "positional argument" in msg
        or ("takes" in msg and "arguments" in msg)
    )


def _tools_of(req: ProviderRequest) -> list[dict[str, Any]] | None:
    """从 `req.func_tool` 提取 OpenAI tools 格式。

    🔴🔴 **2026-09-21 修：这个函数以前**从不返回任何东西**（静默丢工具）。

    原来的实现是：

    ```python
    if hasattr(ft, "get_all"):
        return [t.to_openai_schema() for t in ft.get_all()]   # ← AttributeError!
    except Exception:
        return None                                            # ← 被吞掉
    ```

    ⚠️ `FunctionTool` **没有** `to_openai_schema()` 方法（实测：真实现只有
       `ToolSet.openai_schema()`，`tool.py:203`）→ 每个元素都 `AttributeError`
       → 被 `except Exception` 吞掉 → **恒返回 `None`**。

    ⇒ 走 `build_main_agent` 主链的那些调用点（插件 **9 个文件**在用）
      **永远拿不到工具**，而且**一行日志都没有**（典型的静默降级）。

    ✅ 现在委托给唯一入口 `response_bridge.normalize_tools`（它认
       `ToolSet` / list[dict] / list[FunctionTool] 三种形状，认不出会告警）。
    """
    from astrbot_host.response_bridge import normalize_tools

    return normalize_tools(getattr(req, "func_tool", None))


# ⚠️ 这里原来有两个私有取值函数 `_extract_text` / `_extract_reasoning`，
#    2026-09-21 **已删除**（别再写回来）。原因：
#
#      * 它们是本模块**第二份**"从上游响应里取值"的实现（第一份在
#        `provider.py`），而两者行为**不一致**：
#        `_extract_reasoning` 读 `raw.choices[0].message.reasoning_content`，
#        而我们的返回值是领域层 `ChatResult`（**根本没有 `.choices`**）
#        → 它**恒定返回 None**，推理内容 100% 丢失且无任何报错。
#      * `_extract_text` 也不认 `ChatResult.tool_calls`。
#
#    → 统一收归 `response_bridge`（**唯一入口**）。这条正是本项目最贵的
#      那类 bug：「同一语义有多处实现，其中一处悄悄错」。


async def build_main_agent(
    *,
    event: Any,
    plugin_context: Any,
    config: MainAgentBuildConfig,
    provider: Any = None,
    req: ProviderRequest | None = None,
    apply_reset: bool = True,
) -> MainAgentBuildResult | None:
    """构建主对话代理（照真实现签名与返回类型）。

    Returns:
        `MainAgentBuildResult`；**无可用模型时返回 None**（照真实现），
        并在 event 上设置错误信息（键 `llm_error_message`）。

    ⚠️ `apply_reset` 保留但**不做实际 reset**（我们没有 agent 状态持久化）。
    """
    # 照真实现：没有 req 时先看 event 上有没有
    if req is None:
        cached = event.get_extra("provider_request") if hasattr(event, "get_extra") else None
        if cached is not None:
            req = cached
        else:
            # 兜底：用 event 的消息文本造一个（真实现也支持这条路）
            req = ProviderRequest(
                prompt=getattr(event, "message_str", "") or "",
                session_id=getattr(event, "session_id", None),
            )

    llm = _resolve_llm(plugin_context)
    if llm is None:
        logger.info("宿主未接入模型能力，跳过 LLM 请求处理。")
        if hasattr(event, "set_extra") and not event.get_extra(LLM_ERROR_MESSAGE_EXTRA_KEY):
            event.set_extra(
                LLM_ERROR_MESSAGE_EXTRA_KEY,
                "LLM 请求失败：宿主未接入模型能力（HostPorts.llm 为空）。"
                "请在装配时注入 LLMPort 适配器。",
            )
        return None

    _merge_plugin_tools(req, plugin_context)

    runner = _AgentRunner(llm=llm, req=req, config=config, event=event)
    return MainAgentBuildResult(
        agent_runner=runner,
        provider_request=req,
        provider=provider if provider is not None else llm,
    )


def _merge_plugin_tools(req: ProviderRequest, plugin_context: Any) -> int:
    """把**插件注册的 LLM 工具**并进 `req.func_tool`（照真实现 `:599-617`）。

    ⭐⭐ **为什么这一步缺了整个 function calling 就是断的**：

    真实现（`core/astr_main_agent.py:599-617`，在 `_ensure_persona_and_skills` 内）：

    ```python
    tmgr = plugin_context.get_llm_tool_manager()
    if (persona and persona.get("tools") is None) or not persona:
        persona_toolset = tmgr.get_full_tool_set()
        for tool in list(persona_toolset):
            if not tool.active:
                persona_toolset.remove_tool(tool.name)
    ...
    if not req.func_tool:
        req.func_tool = persona_toolset
    else:
        req.func_tool.merge(persona_toolset)
    ```

    🔴 而**插件自己只做"删"不做"加"** —— 它假设宿主已经把工具填好了：
      * `main.py:18914-18951 _scope_photo_generation_tool_for_request`：
        `req.func_tool` 为 None 直接 `return False`（**静默 no-op**）；
      * `main.py:18964 _annotate_photo_tool_prompt_format_for_request`：同上；
      * `main.py:18254`：`if getattr(req, "func_tool", None) is None: return False`。
    ⇒ 我们**不做这一步**时，插件所有"按会话裁剪/标注工具"的逻辑静默失效，
      而且模型**一个工具都看不到**（工具执行链的上游就断了）。

    Returns:
        并入的工具数（诊断用）。
    """
    manager = None
    getter = getattr(plugin_context, "get_llm_tool_manager", None)
    if callable(getter):
        try:
            manager = getter()
        except Exception:  # noqa: BLE001
            logger.error("取 LLM 工具管理器失败（本轮不带工具）", exc_info=True)
            return 0
    if manager is None or not hasattr(manager, "get_full_tool_set"):
        # ⚠️ 如实记日志（不静默）：这是"工具链断掉"的关键节点
        logger.warning(
            "工具管理器没有 get_full_tool_set（插件注册的工具无法交给模型）"
        )
        return 0

    tool_set = manager.get_full_tool_set()
    # 照真实现：**按 active 过滤**（停用的工具不该给模型）
    for tool in list(tool_set):
        if not getattr(tool, "active", True):
            tool_set.remove_tool(getattr(tool, "name", ""))
    if tool_set.empty():
        return 0

    if not req.func_tool:
        req.func_tool = tool_set
        count = len(tool_set)
    else:
        before = len(req.func_tool)
        req.func_tool.merge(tool_set)
        count = max(0, len(req.func_tool) - before)
    logger.info(
        "已把插件注册的工具并入本轮请求：新增 %s 个，共 %s 个（%s）",
        count,
        len(req.func_tool),
        ", ".join(req.func_tool.names()[:12]),
    )
    return count


def _resolve_llm(plugin_context: Any) -> Any:
    """从 Context 取 LLMPort（走宿主端口，不直接摸真实现）。"""
    ports = getattr(plugin_context, "_ports", None)
    if ports is not None:
        return ports.llm
    return None


def capabilities() -> dict[str, Any]:
    """如实报告本模块的实现程度（供诊断，不夸大）。"""
    return {
        "single_turn": True,
        "streaming": False,          # ⚠️ 整流后单块产出
        "tool_loop": "single_round",  # ⚠️ 非真实现的多轮工具循环
        "context_compression": False,
        "provider_selection": False,
        "checkpoint": False,
    }


__all__ = [
    "LLM_ERROR_MESSAGE_EXTRA_KEY",
    "MainAgentBuildConfig",
    "MainAgentBuildResult",
    "build_main_agent",
    "capabilities",
]
