# -*- coding: utf-8 -*-
"""可替换接口（Protocol）。

🔴 **本节的设计原则（情绪 2026-09-18 强调）**：
   「最终还是要**以陪伴为核心**，**接口别写死了**。」

   背景：我们最终要与陪伴插件（作者正在做新框架，将支持"直接搬到新平台或独立"）对接，
   而**它的机制还在演进**。如果现在把接口写死成某一种具体形态
   （OpenAI 格式 / function calling / 某个生图端点），
   将来对接就要大改。

   所以：**领域层只依赖下面的 Protocol**，具体实现可换。

---

## 三层可替换点

| 层 | Protocol | 当前实现 | 将来可换成 |
|---|---|---|---|
| **对话上游** | `ChatBackend` | `OpenAIChatBackend` | 陪伴的 provider 抽象 / 其他协议 |
| **生图后端** | `ImageBackend` | `OpenAIImageBackend` | NAI / ComfyUI / SD WebUI / 陪伴的生图插件 |
| **工具下发** | `ToolDispatch` | `FunctionCallDispatch` | 文本标签 / 陪伴的工具协议 |

## 为什么是 Protocol 而不是 ABC

`typing.Protocol` 是**结构化类型**（鸭子类型）：实现方**不需要继承我们的类**，
只要方法签名对得上就能用。这对"对接别人的框架"尤其重要 ——
陪伴插件的类不会（也不该）继承我们的基类。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from .message import Message
from .tools import ToolCall, ToolResult


# ===========================================================================
# 1. 对话上游
# ===========================================================================


@runtime_checkable
class ChatBackend(Protocol):
    """对话后端。

    ⚠️ **不假设是 OpenAI 格式**。领域层只调这两个方法，
       具体是 HTTP / 进程内调用 / 别人的 SDK，由实现决定。
    """

    async def open(self) -> None:
        """建立资源（连接池等）。幂等。"""
        ...

    async def close(self) -> None:
        """释放资源。"""
        ...

    async def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 60.0,
        tools: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        """非流式对话。返回带 `content` / `tool_calls` / `finish_reason` 的结果对象。"""
        ...

    def chat_stream(
        self,
        messages: list[Message],
        *,
        model: str,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 60.0,
        tools: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[Any]:
        """流式对话。返回异步迭代器。"""
        ...


# ===========================================================================
# 2. 生图后端
# ===========================================================================


@runtime_checkable
class ImageBackend(Protocol):
    """生图后端。

    ⚠️ **不假设是 OpenAI `/images/generations`**。
       实测陪伴插件支持多种后端（NAI / ComfyUI / SD WebUI / 在线 API），
       而爱语用的是 OpenAI 兼容端点。**两者都要能接**。
    """

    @property
    def name(self) -> str:
        """后端名（给日志与诊断用）。"""
        ...

    def is_configured(self) -> bool:
        """是否已配置好（未配置时工具应返回 `not_configured` 而不是崩）。"""
        ...

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def generate(
        self,
        *,
        prompt: str,
        kind: str = "text2img",
        size: str = "",
        reference_image: str = "",
        n: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        """生成图片。

        Args:
            prompt: 画面描述。
            kind: `text2img` / `selfie` / `edit`。
            size: 尺寸，空则用后端默认。
            reference_image: 参考图（路径或 URL）；`edit` 必填。
            n: 张数。

        Returns:
            带 `images: list[GeneratedImage]` 的结果对象。
        """
        ...


# ===========================================================================
# 3. 工具下发
# ===========================================================================


@runtime_checkable
class ToolDispatch(Protocol):
    """工具下发方式。

    ⭐ **这是情绪特别关心的点**（"工具下发方式"）：
       爱语用**文本标签**（`<generate_image:描述>`，易截断），
       陪伴插件用**原生 function calling**。
       我们实测上游支持 function calling，**默认用它**，
       但**不写死** —— 保留换成其他协议的能力。
    """

    @property
    def name(self) -> str:
        """下发方式名（`function_call` / `text_tag` / …）。"""
        ...

    def build_payload(
        self,
        tools: list[Any],
    ) -> dict[str, Any]:
        """把工具定义变成**请求体里的字段**。

        Returns:
            要 merge 进请求体的 dict。例如 function calling 返回
            `{"tools": [...], "tool_choice": "auto"}`；
            文本标签方式返回 `{}`（因为工具说明走提示词）。
        """
        ...

    def extract_calls(self, response: Any) -> list[ToolCall]:
        """从**非流式响应**里取出工具调用。"""
        ...

    def extract_stream_calls(self, fragments: list[Any]) -> list[ToolCall]:
        """从**流式分片**里聚合成完整的工具调用。"""
        ...

    def render_result(self, result: ToolResult) -> Message:
        """把工具结果变成要回填给模型的消息。"""
        ...

    def prompt_sections(self, tools: list[Any]) -> list[Any]:
        """工具说明要注入的提示词片段（文本标签方式需要，function calling 为空）。"""
        ...
