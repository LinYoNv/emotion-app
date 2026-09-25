# -*- coding: utf-8 -*-
"""`astrbot.api.all` —— 一次性导出全部常用 API。

⭐ 依据：真 AstrBot `api/all.py`（`from astrbot.api.all import *` 是一次性引入写法）。
   实测 `meme_manager` 用它 → 缺这个模块**插件加载失败**。

⚠️ 真实现还导出 `CommandResult` / `EventResultType` / `html_renderer` 等；
   我们**只导出已实现的**（缺的会 ImportError，不假装存在）。
"""
# ruff: noqa: F401, F403
from astrbot_host.compat.api import AstrBotConfig, llm_tool, logger
from astrbot_host.compat.api.event import (
    AstrMessageEvent,
    MessageChain,
    MessageEventResult,
    ResultContentType,
    filter,
)
from astrbot_host.compat.api.message_components import *  # noqa: F403
from astrbot_host.compat.api.provider import LLMResponse, ProviderRequest
from astrbot_host.compat.api.star import Context, Star, StarTools, register
from astrbot_host.compat.core.message.message_event_result import (
    MessageChain as _MC,
    MessageEventResult as _MER,
    ResultContentType as _RCT,
)
from astrbot_host.compat.core.star.register import (
    register_after_message_sent as after_message_sent,
    register_command as command,
    register_command_group as command_group,
    register_custom_filter as custom_filter,
    register_event_message_type as event_message_type,
    register_llm_tool as llm_tool_,
    register_on_agent_begin as on_agent_begin,
    register_on_agent_done as on_agent_done,
    register_on_astrbot_loaded as on_astrbot_loaded,
    register_on_decorating_result as on_decorating_result,
    register_on_llm_request as on_llm_request,
    register_on_llm_response as on_llm_response,
    register_on_llm_tool_respond as on_llm_tool_respond,
    register_on_platform_loaded as on_platform_loaded,
    register_on_plugin_error as on_plugin_error,
    register_on_plugin_loaded as on_plugin_loaded,
    register_on_plugin_unloaded as on_plugin_unloaded,
    register_on_using_llm_tool as on_using_llm_tool,
    register_on_waiting_llm_request as on_waiting_llm_request,
    register_permission_type as permission_type,
    register_platform_adapter_type as platform_adapter_type,
    register_regex as regex,
)

#: 真实现里 `llm_tool` 这个名字同时指装饰器（覆盖上面的 import）
llm_tool = llm_tool_

__all__ = [
    "AstrBotConfig", "AstrMessageEvent", "Context", "LLMResponse",
    "MessageChain", "MessageEventResult", "ProviderRequest",
    "ResultContentType", "Star", "StarTools",
    "after_message_sent", "command", "command_group", "custom_filter",
    "event_message_type", "filter", "llm_tool", "logger", "on_agent_begin",
    "on_agent_done", "on_astrbot_loaded", "on_decorating_result",
    "on_llm_request", "on_llm_response", "on_llm_tool_respond",
    "on_platform_loaded", "on_plugin_error", "on_plugin_loaded",
    "on_plugin_unloaded", "on_using_llm_tool", "on_waiting_llm_request",
    "permission_type", "platform_adapter_type", "register", "regex",
]
