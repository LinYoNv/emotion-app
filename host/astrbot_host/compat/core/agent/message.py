# -*- coding: utf-8 -*-
"""`astrbot.core.agent.message` 兼容层 —— agent 侧消息与内容段。

⭐⭐ **字段照真 AstrBot `core/agent/message.py` 对齐**。
   实测依赖面：`TextPart`(21 文件) / `AssistantMessageSegment`(19) /
   `UserMessageSegment`(17) / `SystemMessageSegment` / `ToolCall` 等
   —— 都在**同一个 `event_dispatch.py`** 里 import（天然适配层）。

⚠️ **重要差异（必须记录）**：真实现基于 **pydantic `BaseModel`**；
   我们不引入 pydantic（依赖刻意只有 quart/hypercorn/aiosqlite/httpx），
   用 `dataclass` + 手写 `model_dump()` 等价替代。

   | 能力 | 真实现(pydantic) | 我们(dataclass) |
   |---|---|---|
   | 构造校验（如"content 必填"） | ✅ 自动抛 ValidationError | ⚠️ 不做（见下） |
   | `model_dump()` | ✅ | ✅ 已实现（形状一致） |
   | `model_dump(exclude_none=True)` | ✅ | ✅ 支持该关键字 |
   | JSON 序列化 | ✅ | ✅ 用 dataclasses.asdict |

   🔴 **为什么不做构造校验**：pydantic 会在构造时抛错，而我们是"宿主兼容层"——
      **宁可让插件拿到它自己传的值，也不要在宿主侧替它做严格判断**。
     若插件依赖 ValidationError，会在别处暴露（我们选择让错误可见而非静默改数据）。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

#: checkpoint 角色（照真实现 `CHECKPOINT_ROLE`）
CHECKPOINT_ROLE = "_checkpoint"


class _DumpMixin:
    """提供 `model_dump()`（pydantic 兼容形状）。"""

    def model_dump(self, *, exclude_none: bool = False, **_: Any) -> dict[str, Any]:
        out = dataclasses.asdict(self)  # type: ignore[call-overload]
        # 去掉私有/内部字段
        out.pop("_no_save", None)
        if exclude_none:
            out = {k: v for k, v in out.items() if v is not None}
        return out


# ---------------------------------------------------------------------------
# 内容段（ContentPart 一族）
# ---------------------------------------------------------------------------


@dataclass
class ContentPart(_DumpMixin):
    """内容段基类（照真实现）。"""

    type: str = ""


@dataclass
class TextPart(ContentPart):
    """文本段。

    >>> TextPart(text="Hello, world!").model_dump()
    {'type': 'text', 'text': 'Hello, world!'}
    """

    text: str = ""
    type: str = "text"


@dataclass
class ThinkPart(ContentPart):
    """推理段（思维链）。"""

    think: str = ""
    type: str = "think"


@dataclass
class ImageURLPart(ContentPart):
    """图片段（URL 形态）。"""

    image_url: dict[str, Any] = field(default_factory=dict)
    type: str = "image_url"


@dataclass
class AudioURLPart(ContentPart):
    """音频段（URL 形态）。"""

    audio_url: dict[str, Any] = field(default_factory=dict)
    type: str = "audio_url"


@dataclass
class ToolCall(_DumpMixin):
    """工具调用（照真实现字段）。"""

    id: str = ""
    type: str = "function"
    function: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallPart(ContentPart):
    """工具调用内容段。"""

    tool_call: ToolCall | None = None
    type: str = "tool_call"


@dataclass
class CheckpointData(_DumpMixin):
    """checkpoint 数据（照真实现 —— 持久化对话用）。"""

    id: str = ""
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Message 一族
# ---------------------------------------------------------------------------


@dataclass
class Message(_DumpMixin):
    """一条对话消息（照真实现字段）。

    ⚠️ 真实现的 `role` 是 `Literal[...]` 且带 `model_validator` 校验；
       我们用普通 str（**不做构造校验**，理由见模块 docstring）。
    """

    role: str = "user"
    content: str | list[ContentPart] | CheckpointData | None = None
    tool_calls: list[ToolCall] | list[dict] | None = None
    tool_call_id: str | None = None

    def to_openai(self) -> dict[str, Any]:
        """转成 OpenAI `messages` 元素（宿主调用模型用）。

        ⚠️ 与 `model_dump()` 的区别：这里**只保留上游认识的字段**
           （真实现的 `_no_save` 之类内部字段不能发出去）。
        """
        out: dict[str, Any] = {"role": self.role}
        content = self.content
        if isinstance(content, list):
            out["content"] = [
                c.model_dump() if isinstance(c, _DumpMixin) else c for c in content
            ]
        elif isinstance(content, CheckpointData):
            out["content"] = content.data.get("text", "")
        else:
            out["content"] = content
        if self.tool_calls is not None:
            out["tool_calls"] = [
                tc.model_dump() if isinstance(tc, _DumpMixin) else tc
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        return out


@dataclass
class AssistantMessageSegment(Message):
    """助手消息段（照真实现）。"""

    role: str = "assistant"


@dataclass
class ToolCallMessageSegment(Message):
    """工具调用结果消息段（照真实现 role='tool'）。"""

    role: str = "tool"


@dataclass
class UserMessageSegment(Message):
    """用户消息段（照真实现）。"""

    role: str = "user"


@dataclass
class SystemMessageSegment(Message):
    """系统消息段（照真实现）。"""

    role: str = "system"


@dataclass
class CheckpointMessageSegment(Message):
    """checkpoint 消息段（照真实现 role='_checkpoint'）。"""

    role: str = CHECKPOINT_ROLE
    content: CheckpointData | None = None


# ---------------------------------------------------------------------------
# checkpoint 辅助函数（照真实现签名）
# ---------------------------------------------------------------------------


def is_checkpoint_message(message: Message | dict) -> bool:
    if isinstance(message, dict):
        return message.get("role") == CHECKPOINT_ROLE
    return message.role == CHECKPOINT_ROLE


def get_checkpoint_id(message: Message | dict) -> str | None:
    if isinstance(message, dict):
        content = message.get("content")
        return content.get("id") if isinstance(content, dict) else None
    if isinstance(message.content, CheckpointData):
        return message.content.id
    return None


def strip_checkpoint_messages(history: list[dict]) -> list[dict]:
    """去掉历史里的 checkpoint 消息（照真实现）。"""
    return [m for m in history if not is_checkpoint_message(m)]


def bind_checkpoint_messages(history: list[dict]) -> list[Message]:
    """把 dict 历史转成 `Message` 列表（照真实现签名）。

    ⚠️ 真实现会在 checkpoint 上绑定后继消息；我们**保持顺序不变地转换**
       （语义差异：不做绑定，但不丢消息）。
    """
    out: list[Message] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        out.append(
            Message(
                role=m.get("role", "user"),
                content=m.get("content"),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
            )
        )
    return out


def dump_messages_with_checkpoints(messages: list[Message]) -> list[dict]:
    """`Message` 列表 → dict 列表（照真实现签名）。"""
    return [m.model_dump() for m in messages if isinstance(m, _DumpMixin)]


__all__ = [
    "CHECKPOINT_ROLE",
    "AssistantMessageSegment",
    "AudioURLPart",
    "CheckpointData",
    "CheckpointMessageSegment",
    "ContentPart",
    "ImageURLPart",
    "Message",
    "SystemMessageSegment",
    "TextPart",
    "ThinkPart",
    "ToolCall",
    "ToolCallMessageSegment",
    "ToolCallPart",
    "UserMessageSegment",
    "bind_checkpoint_messages",
    "dump_messages_with_checkpoints",
    "get_checkpoint_id",
    "is_checkpoint_message",
    "strip_checkpoint_messages",
]
