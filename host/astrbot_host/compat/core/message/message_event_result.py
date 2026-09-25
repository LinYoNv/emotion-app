# -*- coding: utf-8 -*-
"""`astrbot.core.message.message_event_result` —— 事件结果与消息链。

⭐ 依据：真 AstrBot `core/message/message_event_result.py`。
   `MessageEventResult` 继承 `MessageChain` 并加结果标记。

⚠️ 真实现里 `MessageChain` **就在这个模块**（`core/message/message_event_result.py`），
   同时 `core/platform/astr_message_event.py` 也导出它 ——
   两处 import 路径都要能走通（插件两种写法都有）。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from astrbot_host.compat.core.platform.astr_message_event import MessageChain


class EventResultType(enum.Enum):
    """事件传播结果类型（照真实现 `message_event_result.py:195-206`）。

    ⚠️ 成员名逐字一致 —— 插件会 `EventResultType.STOP` 这样引用。
    """

    CONTINUE = enum.auto()
    """事件将会继续传播"""
    STOP = enum.auto()
    """事件将会终止传播"""


class ResultContentType(enum.Enum):
    """结果内容类型（照真实现，成员名逐字一致）。"""

    LLM_RESULT = enum.auto()
    """调用 LLM 产生的结果"""
    AGENT_RUNNER_ERROR = enum.auto()
    """第三方 Agent Runner 返回的错误结果"""
    GENERAL_RESULT = enum.auto()
    """普通的消息结果"""
    STREAMING_RESULT = enum.auto()
    """调用 LLM 产生的流式结果"""
    STREAMING_FINISH = enum.auto()
    """流式输出完成"""


@dataclass
class MessageEventResult(MessageChain):
    """事件结果（照真实现 —— 它是 MessageChain 的子类）。"""

    #: 事件传播结果（真实现 `:237-239`）—— `stop_event()` 写它
    result_type: EventResultType | None = field(
        default_factory=lambda: EventResultType.CONTINUE
    )
    result_content_type: ResultContentType | None = field(
        default_factory=lambda: ResultContentType.GENERAL_RESULT
    )
    #: 异步流（真实现 `:242`）
    async_stream: Any = None

    def __post_init__(self) -> None:
        # dataclass 继承：确保 chain 一定存在
        if not hasattr(self, "chain") or self.chain is None:
            self.chain = []

    def message(self, message: str) -> "MessageEventResult":
        """追加纯文本（照真实现的链式 API）。"""
        from .components import Plain

        self.chain.append(Plain(message))
        return self

    def stop_event(self) -> "MessageEventResult":
        """终止事件传播（真 `:246-249`）—— 置 `result_type`，返回 self。"""
        self.result_type = EventResultType.STOP
        return self

    def continue_event(self) -> "MessageEventResult":
        """继续事件传播（真 `:251-254`）。"""
        self.result_type = EventResultType.CONTINUE
        return self

    def is_stopped(self) -> bool:
        """是否终止传播（真 `:256-258`）。

        ⚠️ 这是**方法**，读的是 `result_type` —— 不是布尔字段。
        """
        return self.result_type == EventResultType.STOP

    def set_async_stream(self, stream: Any) -> "MessageEventResult":
        """设置异步流（真 `:260-263`）。"""
        self.async_stream = stream
        return self

    def set_result_content_type(
        self, typ: ResultContentType
    ) -> "MessageEventResult":
        self.result_content_type = typ
        return self

    def is_llm_result(self) -> bool:
        return self.result_content_type == ResultContentType.LLM_RESULT

    def is_model_result(self) -> bool:
        """真实现里把 LLM 结果与 agent 错误都算"模型产生"（照抄语义）。"""
        return self.result_content_type in (
            ResultContentType.LLM_RESULT,
            ResultContentType.AGENT_RUNNER_ERROR,
        )

    def __str__(self) -> str:
        return "".join(getattr(c, "text", "") or "" for c in self.chain)


#: 为了兼容旧版代码，真实现保留 `CommandResult` 作为 `MessageEventResult` 的**别名**
#: （`message_event_result.py:287-288` 原话「为了兼容旧版代码」）。
#: ⚠️ 它是**别名**不是子类 —— `CommandResult is MessageEventResult` 为真。
#:    真实现的 `call_handler` 用 `isinstance(ret, MessageEventResult | CommandResult)`
#:    判断，别名成立时这个 Union 其实只有一个成员。
CommandResult = MessageEventResult

__all__ = [
    "CommandResult",
    "EventResultType",
    "MessageChain",
    "MessageEventResult",
    "ResultContentType",
]
