# -*- coding: utf-8 -*-
"""`astrbot.core.star.register` —— 装饰器导出面。

⭐ 依据：真 AstrBot `core/star/register/__init__.py`。
   ⚠️ **导出名必须逐字一致** —— 插件的 `@filter.xxx` 就是从这里来的。
"""
from astrbot_host.compat.core.star.register.star import register_star
from astrbot_host.compat.core.star.register.star_handler import (
    register_after_message_sent as register_after_message_sent,
    register_command as register_command,
    register_command_group as register_command_group,
    register_custom_filter as register_custom_filter,
    register_event_message_type as register_event_message_type,
    register_llm_tool as register_llm_tool,
    register_on_agent_begin as register_on_agent_begin,
    register_on_agent_done as register_on_agent_done,
    register_on_astrbot_loaded as register_on_astrbot_loaded,
    register_on_decorating_result as register_on_decorating_result,
    register_on_llm_request as register_on_llm_request,
    register_on_llm_response as register_on_llm_response,
    register_on_llm_tool_respond as register_on_llm_tool_respond,
    register_on_platform_loaded as register_on_platform_loaded,
    register_on_plugin_error as register_on_plugin_error,
    register_on_plugin_loaded as register_on_plugin_loaded,
    register_on_plugin_unloaded as register_on_plugin_unloaded,
    register_on_using_llm_tool as register_on_using_llm_tool,
    register_on_waiting_llm_request as register_on_waiting_llm_request,
    register_permission_type as register_permission_type,
    register_platform_adapter_type as register_platform_adapter_type,
    register_regex as register_regex,
)

__all__ = [
    "register_agent",
    "register_star",
    "register_after_message_sent",
    "register_command",
    "register_command_group",
    "register_custom_filter",
    "register_event_message_type",
    "register_llm_tool",
    "register_on_agent_begin",
    "register_on_agent_done",
    "register_on_astrbot_loaded",
    "register_on_decorating_result",
    "register_on_llm_request",
    "register_on_llm_response",
    "register_on_llm_tool_respond",
    "register_on_platform_loaded",
    "register_on_plugin_error",
    "register_on_plugin_loaded",
    "register_on_plugin_unloaded",
    "register_on_using_llm_tool",
    "register_on_waiting_llm_request",
    "register_permission_type",
    "register_platform_adapter_type",
    "register_regex",
]
