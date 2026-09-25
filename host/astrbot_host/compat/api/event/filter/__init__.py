# -*- coding: utf-8 -*-
"""`astrbot.api.event.filter` —— **14 个装饰器的公开入口**。

⭐ 依据：真 AstrBot `api/event/filter/__init__.py`（实测 72 行）。
   ⚠️ **导出名必须逐字一致** —— 插件的 `@filter.llm_tool` / `@filter.on_llm_request`
      等全部来自这里。少一个 → 插件**加载即失败**。
"""
from astrbot_host.compat.core.star.register import (
    register_after_message_sent as after_message_sent,
    register_command as command,
    register_command_group as command_group,
    register_custom_filter as custom_filter,
    register_event_message_type as event_message_type,
    register_llm_tool as llm_tool,
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

from astrbot_host.compat.core.star.filter.custom_filter import CustomFilter
from astrbot_host.compat.core.star.filter.event_message_type import (
    EventMessageType,
    EventMessageTypeFilter,
)
from astrbot_host.compat.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot_host.compat.core.star.filter.platform_adapter_type import (
    PlatformAdapterType,
    PlatformAdapterTypeFilter,
)

__all__ = [
    "CustomFilter",
    "EventMessageType",
    "EventMessageTypeFilter",
    "PermissionType",
    "PermissionTypeFilter",
    "PlatformAdapterType",
    "PlatformAdapterTypeFilter",
    "after_message_sent",
    "command",
    "command_group",
    "custom_filter",
    "event_message_type",
    "llm_tool",
    "on_agent_begin",
    "on_agent_done",
    "on_astrbot_loaded",
    "on_decorating_result",
    "on_llm_request",
    "on_llm_response",
    "on_llm_tool_respond",
    "on_platform_loaded",
    "on_plugin_error",
    "on_plugin_loaded",
    "on_plugin_unloaded",
    "on_using_llm_tool",
    "on_waiting_llm_request",
    "permission_type",
    "platform_adapter_type",
    "regex",
]
