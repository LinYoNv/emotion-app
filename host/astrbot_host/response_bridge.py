# -*- coding: utf-8 -*-
"""把宿主各条 LLM 返回路径**统一**包成 AstrBot 的 `LLMResponse`。

⭐⭐ **为什么必须只有这一份实现**（这是本项目最贵的一课，见 AGENTS.md
   「同一概念有多条写入路径」）：

   宿主有**三条**把上游返回值交给插件的路：

     ① `Provider.text_chat()`            （插件按 provider 调模型）
     ② `Context._as_llm_response()`      （插件没指定 provider 时走 HostPorts.llm）
     ③ `astr_main_agent._AgentRunner.run()`（插件走 build_main_agent 那条主链）

   三条路**各自拼了一个 `LLMResponse`**，于是同一个缺口在三个地方各犯一次：

```python
resp = LLMResponse(completion_text=_extract_text(out))   # ← 只搬了文本
```

🔴 **后果（实测，不是推测）**：`tool_calls` 与 `reasoning_content` 被**静默丢弃**。
   `ChatResult` 上明明有 `tool_calls`（`emotion_app/domain/upstream.py:96`），
   而插件读的是 `resp.tools_call_name` / `resp.tools_call_args`
   （实测消费点：`main.py:19298/:19363`、`llm_tool_actions.py:2474`、
   `daily_state.py:14764`、`hdsi_experiment.py:573`）。

   插件的工具闭环判定是：

```python
tool_names = getattr(resp, "tools_call_name", None)   # main.py:19298
...
response_has_tool_call = bool(getattr(resp, "tools_call_name", None))  # :19363
```

   → 恒为 `[]` ⇒ **插件永远认为"模型没调工具"** ⇒ 它的 function calling
   能力在我们的宿主上**完全失效**，而且**一行日志都不会有**
   （`getattr` 带默认值 = 静默降级，正是本项目最贵的那类 bug）。

## 与真实现对齐的细节（照 `openai_source.py:881-925` 抄）

真 AstrBot 的 `text_chat` 在解析完响应后做这几件事（我们逐条对齐）：

| 真实现行为 | 出处 | 我们照做 |
|---|---|---|
| `content` 非 None → 只填 `result_chain`（**不**填 `_completion_text`） | `:856-872` | ✅ 走 `LLMResponse(result_chain=…)` |
| `tool_calls` 存在**且** `tools is not None` 才解析 | `:881` | ✅ 同样要求 `tools` 非空 |
| `arguments` 是 JSON **字符串** → `json.loads` 成 dict | `:895-903` | ✅ |
| 解析失败 → 记 ERROR 并用 `{}`（**不**中断） | `:900-903` | ✅ |
| `args is None` → `{}`（无参工具） | `:905-906` | ✅ |
| 有工具调用 → `role = "tool"` | `:925` | ✅ |
| 三个字段一起填：`args` / `name` / `ids` | `:918-920` | ✅ |
| 「文本+推理+工具」**三者全空** → 报"没有可用输出" | `:930-943` | ✅ 打 ERROR（见下） |

⚠️ **`role="tool"` 这一条容易漏**：插件有按 `role` 分支的逻辑
   （`token_budget.py:1566` 读 `response.role`），
   真实现只在**有工具调用**时才置 `"tool"`。

## 🔴 一处**刻意的不对齐**：不抛 `EmptyModelOutputError`

真实现在"三者全空"时 `raise EmptyModelOutputError`（`openai_source.py:935`）。
我们**不抛**，改为打 ERROR 并返回空 `LLMResponse`，理由：

  * 插件的 `token_budget` 对**异常**与**空回复**走**两条不同的**分支
    （`token_budget.py:1553-1560` 只捕获超时异常；空文本走敏感词/降级判定）。
    换成抛异常 = 改变插件既有降级路径的行为，风险大于收益；
  * 我们的 compat 层**没有** `astrbot.core.exceptions` 模块，
    抛一个插件不认识的自造异常类型更糟（作者判据③：能力不对等要**如实**，
    不伪造 —— 但"如实"不等于"换个类型炸给上游看"）。

  → 所以：**保持返回类型稳定**，但把"没有可用输出"这件事**响亮地**记下来，
    不再像以前那样把"有工具调用、content 为空"这种**完全正常**的响应
    也误报成"插件将收到空回复"。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("astrbot")


#: 从返回值里取文本时依次尝试的属性名。
#:
#: ⚠️ `content` 在前（领域层 `ChatResult.content`），`completion_text` 在后
#:    （已经是 `LLMResponse` 时）。顺序不能反：某些对象的 `completion_text`
#:    是个 property，读它会触发额外逻辑。
_TEXT_ATTRS: tuple[str, ...] = ("content", "text", "completion_text")

#: 推理内容可能出现的属性名（不同上游/网关叫法不同）。
_REASONING_ATTRS: tuple[str, ...] = (
    "reasoning_content",
    "reasoning",
    "thinking_content",
)


def _first_str(obj: Any, attrs: tuple[str, ...]) -> str:
    """取第一个**非空字符串**属性；取不到返回 `""`。"""
    for attr in attrs:
        try:
            val = getattr(obj, attr, None)
        except Exception:  # noqa: BLE001 — property 抛错不该让整次调用挂掉
            continue
        if isinstance(val, str) and val:
            return val
    return ""


def extract_text(out: Any) -> str:
    """从 `LLMPort.generate()` 的返回值里取**正文文本**。

    ⚠️ 取不到只返回 `""`（由 `to_llm_response` 统一判定"有没有可用输出"）；
       本函数自己**不**告警 —— 否则会被调用两次、日志重复。
    """
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    return _first_str(out, _TEXT_ATTRS)


def extract_reasoning(out: Any) -> str:
    """取推理内容（`reasoning_content` 等）；取不到返回 `""`。"""
    if out is None or isinstance(out, str):
        return ""
    return _first_str(out, _REASONING_ATTRS)


def extract_usage(out: Any) -> Any:
    """取用量（token 统计）。取不到返回 `None`。

    ⚠️ **原样返回，不做形状转换**：插件自己有一套非常宽的探测逻辑
       （`token_budget.py:257-290` 的 `_usage_candidates`，
       会递归 `usage`/`token_usage`/`model_dump()`…），
       我们转成 `TokenUsage` 反而**丢掉**它想读的原始字段
       （`completion_tokens_details.reasoning_tokens` 等）。
    """
    try:
        return getattr(out, "usage", None)
    except Exception:  # noqa: BLE001
        return None


def parse_tool_calls(out: Any, tools: Any) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """从返回值里解析工具调用 → `(args_list, name_list, id_list)`。

    🔴 **`tools is None` 时真实现不解析**（`openai_source.py:881`
       `if choice.message.tool_calls and tools is not None:`）——
       我们**照抄这个前提**：没给工具集却收到 tool_calls 属于协议异常，
       照真实现忽略它，不擅自执行（避免"插件没要求调工具却被调了"）。
    """
    if tools is None:
        return [], [], []
    raw = getattr(out, "tool_calls", None)
    if not isinstance(raw, list) or not raw:
        return [], [], []

    args_ls: list[dict[str, Any]] = []
    name_ls: list[str] = []
    id_ls: list[str] = []

    for call in raw:
        # 有的网关把整条 tool_call 塞成 JSON 字符串（真实现 #1359 的 workaround）
        if isinstance(call, str):
            try:
                call = json.loads(call)
            except (TypeError, ValueError):
                logger.error("工具调用是字符串但解析不出 JSON，已跳过：%r", call[:200])
                continue
        if not isinstance(call, dict):
            continue
        if str(call.get("type") or "") != "function":
            continue

        fn = call.get("function")
        if not isinstance(fn, dict):
            fn = {}

        arguments = fn.get("arguments")
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except (TypeError, ValueError) as exc:
                # 真实现同样"记 ERROR 并用 {}"（`openai_source.py:900-903`）
                logger.error(
                    "解析工具参数失败（工具=%s）：%s；按空参数继续",
                    fn.get("name"),
                    exc,
                )
                args = {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            args = {}
        # 无参工具：上游可能给 None（真实现 `:905-906`）
        if args is None:
            args = {}
        if not isinstance(args, dict):
            args = {}

        args_ls.append(args)
        name_ls.append(str(fn.get("name") or ""))
        id_ls.append(str(call.get("id") or ""))

    return args_ls, name_ls, id_ls


def to_llm_response(out: Any, *, tools: Any = None) -> Any:
    """把上游返回值包成 `LLMResponse`（**保留全部可用字段**）。

    这是**唯一**的转换入口 —— 三条路都必须调它（见模块 docstring）。
    已经是 `LLMResponse` 的**原样返回**（幂等，避免二次包装丢字段）。

    Args:
        out: `LLMPort.generate()` 的返回值（`ChatResult` / `LLMResponse` / str / None）。
        tools: 本次调用带上的工具集；**`None` 表示没带** → 不解析 tool_calls
            （照真实现 `openai_source.py:881`）。

    Returns:
        填好 `completion_text` / `reasoning_content` / `tools_call_*` / `usage`
        的 `LLMResponse`。
    """
    from astrbot_host.compat.core.provider.entities import LLMResponse

    if isinstance(out, LLMResponse):
        return out

    text = extract_text(out)
    reasoning = extract_reasoning(out)
    args_ls, name_ls, id_ls = parse_tool_calls(out, tools)

    resp = LLMResponse(completion_text=text)
    if reasoning:
        resp.reasoning_content = reasoning
    if args_ls or name_ls:
        # 真实现：有工具调用时 role 置 "tool"（`openai_source.py:925`）
        resp.role = "tool"
        resp.tools_call_args = args_ls
        resp.tools_call_name = name_ls
        resp.tools_call_ids = id_ls
    usage = extract_usage(out)
    if usage is not None:
        try:
            resp.usage = usage
        except Exception:  # noqa: BLE001 — usage 是**附加**信息，不该影响回复
            pass

    # ---- "没有可用输出"判定（照真实现 `:930-943`，但**不抛异常**，见模块 docstring） ----
    if not text.strip() and not reasoning.strip() and not args_ls:
        logger.error(
            "模型响应没有任何可用输出（type=%s，finish_reason=%r）—— "
            "插件将收到空回复。这**不是**正常路径：请检查上游是否返回了空 choices；"
            "若是工具调用场景，说明 tool_calls 也没能解析出来",
            type(out).__name__,
            getattr(out, "finish_reason", ""),
        )

    return resp


def normalize_tools(tools: Any) -> list[dict[str, Any]] | None:
    """把插件给的**工具集对象**转成上游要的 OpenAI schema 列表。

    ## 🔴 为什么需要它（2026-09-21 实测真 bug）

    插件把工具集以 **`ToolSet` 对象**（`page_api.py:4217` `ToolSet([trial_tool])`）
    经 `TokenBudgetMixin._llm_tool_call` → `context.llm_generate(tools=...)` 传下来，
    而**上游要的是 OpenAI 格式的 list[dict]**。

    我们原来**原样透传对象**，后果实测（服务日志原文）::

        TypeError: Object of type ToolSet is not JSON serializable
        During handling of the above exception, another exception occurred:
        ...
        ValueError: 上游 base_url 为空：无法拼出请求地址

    ⚠️ **第二段错误完全是误导**：`provider_bridge` 的 `except TypeError`
       本意是"后端签名不匹配"的兜底，却把**序列化 TypeError** 也接住了 →
       用**不带连接参数**的签名重试 → 于是报"base_url 为空"。
       现场看到的错离根因（"工具集没转格式"）**隔了三层**。

    ## 接受的形状（插件实测出现过的都在这里）

    | 形状 | 处理 |
    |---|---|
    | `None` / `[]` | → `None`（**不传** `tools` 键，不是传空数组） |
    | `ToolSet`（有 `openai_schema`） | → 调它 |
    | `ToolSet` 的旧名（有 `get_func_desc_openai_style`） | → 调它 |
    | `list[dict]` | → 原样（已经是 OpenAI 格式） |
    | `list[FunctionTool]` | → 包成 `ToolSet` 再转 |
    | 有 `get_all()` 的其它容器 | → 取出来再按上面处理 |

    ⚠️ **认不出来时返回 `None` 并记 WARNING**（不静默）：
       宁可"这轮没带工具"（插件的降级路径本来就有），
       也不要抛一个把真实原因埋掉的异常。
    """
    if tools is None:
        return None
    if isinstance(tools, (list, tuple)):
        items = list(tools)
        if not items:
            return None
        # 已经是 OpenAI 格式（dict）
        if all(isinstance(t, dict) for t in items):
            return items
        # 是 FunctionTool 对象列表 → 包一层 ToolSet 走同一条转换
        try:
            from astrbot_host.compat.core.agent.tool import ToolSet

            schema = ToolSet(items).openai_schema()
            return schema or None
        except Exception:  # noqa: BLE001
            logger.warning(
                "工具列表形状无法识别（元素类型=%s）—— 本轮按不带工具处理",
                {type(t).__name__ for t in items},
            )
            return None

    # `ToolSet` 等容器对象：优先用真实现的格式转换方法
    for method_name in ("openai_schema", "get_func_desc_openai_style"):
        builder = getattr(tools, method_name, None)
        if callable(builder):
            try:
                schema = builder()
            except Exception:  # noqa: BLE001
                logger.error("调用 %s() 转换工具集失败", method_name, exc_info=True)
                return None
            if isinstance(schema, list) and schema:
                return schema
            return None

    # 兜底：有 `get_all()` 就取出来递归处理（真实现 ToolSet 也有这个方法）
    getter = getattr(tools, "get_all", None)
    if callable(getter):
        try:
            items = list(getter())
        except Exception:  # noqa: BLE001
            items = []
        if items:
            return normalize_tools(items)

    logger.warning(
        "工具集形状无法识别（type=%s，没有 openai_schema/get_all）—— "
        "本轮按不带工具处理。⚠️ 若插件确实要用 function calling，"
        "这就是它静默失效的原因",
        type(tools).__name__,
    )
    return None


__all__ = [
    "extract_reasoning",
    "extract_text",
    "extract_usage",
    "normalize_tools",
    "parse_tool_calls",
    "to_llm_response",
]
