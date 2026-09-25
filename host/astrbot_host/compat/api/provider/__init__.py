# -*- coding: utf-8 -*-
"""`astrbot.api.provider` —— 模型请求/响应对象（**19 个文件用**）。

⭐ 依据：真 AstrBot `api/provider/__init__.py` + `core/provider/entities.py`。

🔴 **`ProviderRequest` 是陪伴最关键的接入点之一**：
   插件通过 `@filter.on_llm_request()` 拿到它，**往里注入 system_prompt /
   contexts / 工具**（实测陪伴的 `inject_hdsi_experiment_prompt` 等就在做这个）。
   所以字段名与形态必须准 —— 写错会让提示词注入静默失效。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderRequest:
    """一次模型请求（照真实现 `core/provider/entities.py:91` 的字段）。"""

    prompt: str | None = None
    """提示词"""
    session_id: str | None = ""
    """会话 ID"""
    image_urls: list[str] = field(default_factory=list)
    """图片 URL 列表"""
    audio_urls: list[str] = field(default_factory=list)
    """音频 URL 列表，也支持本地路径"""
    extra_user_content_parts: list[Any] = field(default_factory=list)
    """额外的用户消息内容部分"""
    func_tool: Any = None
    """可用的函数工具"""
    contexts: list[dict] = field(default_factory=list)
    """OpenAI 格式上下文列表"""
    system_prompt: str = ""
    """系统提示词"""
    conversation: Any = None
    """关联的对话对象"""
    tool_calls_result: Any = None
    """上次请求后工具调用的结果"""
    model: str | None = None
    """模型名称；None 表示用 provider 默认"""

    def append_tool_calls_result(self, tool_calls_result: Any) -> None:
        """追加工具调用结果（照真实现 —— 单对象会先转成列表）。"""
        if not self.tool_calls_result:
            self.tool_calls_result = []
        if not isinstance(self.tool_calls_result, list):
            self.tool_calls_result = [self.tool_calls_result]
        self.tool_calls_result.append(tool_calls_result)

    def __repr__(self) -> str:
        return (
            f"ProviderRequest(prompt={self.prompt!r}, session_id={self.session_id!r}, "
            f"image_count={len(self.image_urls or [])}, "
            f"audio_count={len(self.audio_urls or [])}, "
            f"system_prompt_len={len(self.system_prompt or '')})"
        )

    __str__ = __repr__


@dataclass
class LLMResponse:
    """模型响应（照真实现 `core/provider/entities.py`）。

    ⚠️ 陪伴有 **22 个文件** 用 `astrbot.core.provider.entities.LLMResponse`，
       字段名错就会静默读不到内容。
    """

    role: str = "assistant"
    """角色"""
    completion_text: str = ""
    """补全文本"""
    raw_completion: Any = None
    """原始响应"""
    usage: Any = None
    """用量统计"""
    tools_call_name: list[str] = field(default_factory=list)
    """工具调用名"""
    tools_call_args: list[dict] = field(default_factory=list)
    """工具调用参数"""
    tools_call_ids: list[str] = field(default_factory=list)
    """工具调用 ID"""
    reasoning_content: str | None = None
    """推理内容（思维链）"""

    def __repr__(self) -> str:
        return (
            f"LLMResponse(role={self.role!r}, "
            f"text={self.completion_text[:40]!r}, "
            f"tools={self.tools_call_name})"
        )


__all__ = ["LLMResponse", "ProviderRequest"]
