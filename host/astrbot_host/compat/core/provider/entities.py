# -*- coding: utf-8 -*-
"""`astrbot.core.provider.entities` 兼容层 —— provider 侧实体。

⭐⭐ **照真 AstrBot `core/provider/entities.py` 实现**。
   实测依赖：`LLMResponse` **22 文件**（陪伴最常用的私有 API 之一）。

🔴 **两处必须照抄、极易写错的细节**（都是读真源码才发现的）：
   ① `LLMResponse.completion_text` **不是普通字段**，而是 **property**：
       · getter：**优先返回 `result_chain.get_plain_text()`**，
         只有 `result_chain` 为空时才返回 `_completion_text`；
       · setter：有 `result_chain` 时**改写链**（清掉 Plain 再插到最前），
         否则写 `_completion_text`。
      → 写成普通 dataclass 字段的话，插件设了 `result_chain` 后
        `completion_text` 会读到**空字符串**（静默失效，极难排查）。
   ② `ToolCallsResult` 的字段是 `tool_calls_info` + `tool_calls_result`
      （**不是** `assistant_message` 之类想当然的名字）。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from astrbot_host.compat.core.platform.astr_message_event import MessageChain


class ProviderType(enum.Enum):
    """provider 类型（照真实现）。"""

    CHAT_COMPLETION = "chat_completion"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    EMBEDDING = "embedding"
    RERANK = "rerank"


@dataclass
class ProviderMeta:
    """provider 元信息（照真实现字段）。"""

    id: str = ""
    model: str = ""
    type: str = ""
    provider_type: ProviderType | None = None


@dataclass
class ProviderMetaData(ProviderMeta):
    """provider 元数据（照真实现 —— 继承 ProviderMeta）。"""


@dataclass
class ToolCallsResult:
    """工具调用结果（照真实现字段名）。"""

    tool_calls_info: Any = None
    """`AssistantMessageSegment` —— 助手发起的工具调用"""
    tool_calls_result: list[Any] = field(default_factory=list)
    """`list[ToolCallMessageSegment]` —— 工具返回结果"""

    def to_openai_messages(self) -> list[dict]:
        """转 OpenAI 消息列表（**逐字照**真实现 `entities.py:74-79`）。

        ```python
        ret = [self.tool_calls_info.model_dump(),
               *[item.model_dump() for item in self.tool_calls_result]]
        ```

        🔴 **为什么必须有**：工具循环要把"助手要求调工具 + 工具返回结果"
        作为 **1 条 `role:"assistant"`（带 `tool_calls`）+ N 条 `role:"tool"`
        （带 `tool_call_id`）** 喂回上游。上游（`openai_source.py:983-990`）
        就是靠这个方法拿消息的；没有它，工具结果**永远回不到模型**
        （表现为"工具跑了但模型不知道"，然后重复调同一个工具）。

        ⚠️ 我们的 `tool_calls_info` / `tool_calls_result` 里放的是**普通 dict**
        （我们的循环自己拼的），不是 pydantic 段对象 —— 所以这里对两者都兼容：
        有 `model_dump()` 就调它，否则原样用。
        """
        return [
            _dump_segment(self.tool_calls_info),
            *[_dump_segment(item) for item in self.tool_calls_result],
        ]

    def to_openai_messages_model(self) -> list[Any]:
        """段对象形态（照真实现 `entities.py:81-87`）。"""
        return [self.tool_calls_info, *self.tool_calls_result]


def _dump_segment(item: Any) -> Any:
    """段对象 → dict（兼容 pydantic 段与我们自己拼的 dict）。"""
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump()
    return item


@dataclass
class TokenUsage:
    """用量统计（照真实现字段）。"""

    input_other: int = 0
    input_cached: int = 0
    output: int = 0


@dataclass
class LLMResponse:
    """模型响应（照真实现 —— 含 `completion_text` property 语义）。"""

    role: str = "assistant"
    """角色：assistant / tool / err"""
    result_chain: MessageChain | None = None
    """消息链形态的补全结果"""
    tools_call_args: list[dict[str, Any]] = field(default_factory=list)
    tools_call_name: list[str] = field(default_factory=list)
    tools_call_ids: list[str] = field(default_factory=list)
    tools_call_extra_content: dict[str, dict[str, Any]] = field(default_factory=dict)
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    raw_completion: Any = None
    is_chunk: bool = False
    id: str | None = None
    usage: TokenUsage | None = None
    #: ⚠️ 私有字段 —— 通过下面的 property 访问（照真实现）
    _completion_text: str = ""

    def __init__(
        self,
        role: str = "assistant",
        completion_text: str | None = None,
        result_chain: MessageChain | None = None,
        tools_call_args: list[dict[str, Any]] | None = None,
        tools_call_name: list[str] | None = None,
        tools_call_ids: list[str] | None = None,
        tools_call_extra_content: dict[str, dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        raw_completion: Any = None,
        is_chunk: bool = False,
        id: str | None = None,  # noqa: A002
        usage: TokenUsage | None = None,
        **kwargs: Any,
    ) -> None:
        self.role = role
        # 🔴 **赋值顺序必须照真实现**（`entities.py:371-373`）：
        #    `completion_text` **先**、`result_chain` **后**。
        #    因为 setter 在"已有 result_chain"时会**改写链** ——
        #    若先设链再设文本，构造时传进来的链会被文本改写掉，
        #    与真实现行为不一致（实测对照过：真实现保留原链）。
        self.completion_text = completion_text
        self.result_chain = result_chain
        self.tools_call_args = tools_call_args or []
        self.tools_call_name = tools_call_name or []
        self.tools_call_ids = tools_call_ids or []
        self.tools_call_extra_content = tools_call_extra_content or {}
        self.reasoning_content = reasoning_content
        self.raw_completion = raw_completion
        self.is_chunk = is_chunk
        self.id = id
        self.usage = usage

    @property
    def completion_text(self) -> str:
        """补全文本（**优先 result_chain**，照真实现）。"""
        if self.result_chain:
            getter = getattr(self.result_chain, "get_plain_text", None)
            if callable(getter):
                return getter()
            # 兜底：链没有 get_plain_text 时用 str()
            return str(self.result_chain)
        return self._completion_text

    @completion_text.setter
    def completion_text(self, value: Any) -> None:
        """设置补全文本（照真实现 —— 有链时改写链，否则写私有字段）。"""
        if self.result_chain is not None and value is not None:
            from astrbot_host.compat.core.message.components import Plain

            chain = getattr(self.result_chain, "chain", None)
            if isinstance(chain, list):
                # 清空 Plain 组件，再把新文本插到最前（照真实现）
                self.result_chain.chain = [
                    c for c in chain if not isinstance(c, Plain)
                ]
                self.result_chain.chain.insert(0, Plain(value))
                return
        self._completion_text = value or ""

    def to_openai_tool_calls(self) -> list[dict]:
        """转 OpenAI tool_calls 格式（照真实现）。"""
        ret = []
        for idx, args in enumerate(self.tools_call_args):
            ret.append({
                "id": self.tools_call_ids[idx] if idx < len(self.tools_call_ids) else "",
                "function": {
                    "name": (
                        self.tools_call_name[idx]
                        if idx < len(self.tools_call_name) else ""
                    ),
                    "arguments": args,
                },
                "type": "function",
            })
        return ret

    def __repr__(self) -> str:
        return (
            f"LLMResponse(role={self.role!r}, "
            f"text={self.completion_text[:40]!r}, "
            f"tools={self.tools_call_name})"
        )


@dataclass
class RerankResult:
    """重排结果（照真实现）。"""

    index: int = 0
    score: float = 0.0
    document: Any = None


__all__ = [
    "LLMResponse",
    "ProviderMeta",
    "ProviderMetaData",
    "ProviderType",
    "ProviderRequest",
    "RerankResult",
    "TokenUsage",
    "ToolCallsResult",
]

# ⚠️ `ProviderRequest` 的真身也在真实现的**同一个模块**里
#    （`core/provider/entities.py:91`），所以这里 re-export，
#    保证 `from astrbot.core.provider.entities import ProviderRequest` 能走通。
from astrbot_host.compat.api.provider import ProviderRequest  # noqa: E402
