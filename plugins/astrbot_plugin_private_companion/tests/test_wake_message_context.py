# -*- coding: utf-8 -*-
"""Exercise the real wake stage, request hooks and optional memory consumer."""
from __future__ import annotations

import importlib
import sys
import types
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.astr_main_agent import _apply_prompt_prefix
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.event_message_type import EventMessageType, EventMessageTypeFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
from astrbot_plugin_private_companion.wake_message_context import WAKE_MESSAGE_CONTEXT_ATTR
from test_plugin_command_passthrough import _Event, _OtherPlugin, _handler


@pytest.fixture
def plugin():
    owner = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
    owner.enabled = True
    owner._bot_scope_allows_event = Mock(return_value=True)
    owner.context = SimpleNamespace(get_config=Mock(return_value={
        "platform_settings": {}, "admins_id": [],
        "wake_prefix": ["/", "星缘", "缘缘"], "plugin_set": ["*"],
    }))
    return owner


async def wake(plugin, text, *, private=True):
    event = _Event(text, private=private)
    other = _OtherPlugin()
    handlers = [
        _handler(other, "observe", [EventMessageTypeFilter(EventMessageType.ALL)]),
        _handler(other, "sub_list", [CommandFilter("bili_sub_list", alias={"订阅列表"})]),
    ]
    stage = WakingCheckStage()
    await stage.initialize(SimpleNamespace(astrbot_config=plugin.context.get_config.return_value))
    registry = SimpleNamespace(get_handlers_by_event_type=Mock(return_value=handlers))
    with patch("astrbot.core.pipeline.waking_check.stage.star_handlers_registry", registry), patch(
        "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
        new=AsyncMock(side_effect=lambda event, matched: matched),
    ):
        await stage.process(event)
    assert not event.is_stopped()
    assert not event.sent
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("text", ["星缘呢", "星缘 呢", "  星缘呢  ", "缘缘在吗", "星缘\n你今天好吗"])
async def test_chat_preserves_original_for_llm_without_changing_routing(plugin, private, text):
    event = await wake(plugin, text, private=private)
    routed = event.message_str
    handlers = event.get_extra("activated_handlers")
    params = deepcopy(event.get_extra("handlers_parsed_params"))
    await plugin.preserve_addressed_user_message(event)
    req = ProviderRequest(
        prompt=routed,
        system_prompt="原来的人格规则",
        contexts=[{"role": "user", "content": "呢"}],
        image_urls=["existing-image"],
        extra_user_content_parts=[TextPart(text="原有引用和附件")],
    )
    extra_parts, history, images = req.extra_user_content_parts, req.contexts, req.image_urls
    await plugin.restore_addressed_user_request(event, req)
    await plugin.restore_addressed_user_request(event, req)
    assert req.prompt == text.strip()
    assert event.message_str == event.get_message_str() == routed
    assert event.message_obj.message_str == text
    assert event.get_extra("activated_handlers") is handlers
    assert event.get_extra("handlers_parsed_params") == params
    assert req.contexts is history and history == [{"role": "user", "content": "呢"}]
    assert req.extra_user_content_parts is extra_parts and extra_parts[0].text == "原有引用和附件"
    assert req.image_urls is images
    assert req.system_prompt == "原来的人格规则"
    assert not event.is_stopped() and not event.call_llm


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["用户说：", "<user>{{prompt}}</user>", "{{prompt}} / {{prompt}}"])
async def test_framework_prompt_template_is_retained(plugin, prefix):
    plugin.context.get_config.return_value["provider_settings"] = {"prompt_prefix": prefix}
    event = await wake(plugin, "星缘呢")
    req = ProviderRequest(prompt=event.message_str)
    _apply_prompt_prefix(req, plugin.context.get_config.return_value["provider_settings"])
    await plugin.restore_addressed_user_request(event, req)
    expected = ProviderRequest(prompt="星缘呢")
    _apply_prompt_prefix(expected, {"prompt_prefix": prefix})
    assert req.prompt == expected.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("text", ["星缘订阅列表", "星缘 订阅列表", "/订阅列表", "星缘/bili_sub_list"])
async def test_other_plugin_commands_keep_parsed_text_and_arguments(plugin, private, text):
    event = await wake(plugin, text, private=private)
    routed = event.message_str
    params = deepcopy(event.get_extra("handlers_parsed_params"))
    req = ProviderRequest(prompt=routed)
    await plugin.preserve_addressed_user_message(event)
    await plugin.restore_addressed_user_request(event, req)
    assert not hasattr(event, WAKE_MESSAGE_CONTEXT_ATTR)
    assert req.prompt == event.message_str == routed
    assert event.get_extra("handlers_parsed_params") == params
    assert plugin._message_debounce_command_text(event, routed)
    assert not event.is_stopped()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["呢", "你说星缘呢", "/星缘呢", "在吗"])
async def test_normal_text_and_symbol_wake_prefixes_are_unchanged(plugin, text):
    event = await wake(plugin, text)
    req = ProviderRequest(prompt=event.message_str)
    routed = req.prompt
    await plugin.preserve_addressed_user_message(event)
    await plugin.restore_addressed_user_request(event, req)
    assert not hasattr(event, WAKE_MESSAGE_CONTEXT_ATTR)
    assert req.prompt == routed


@pytest.mark.asyncio
async def test_first_matching_prefix_and_session_config_are_respected(plugin):
    config = plugin.context.get_config.return_value
    config["wake_prefix"] = ["星", "星缘"]
    event = await wake(plugin, "星缘呢")
    await plugin.preserve_addressed_user_message(event)
    assert getattr(event, WAKE_MESSAGE_CONTEXT_ATTR)["wake_prefix"] == "星"
    assert event.message_str == "缘呢"
    plugin.context.get_config.assert_called_with(umo=event.unified_msg_origin)
    config["wake_prefix"] = [""]
    unchanged = await wake(plugin, "星缘呢")
    await plugin.preserve_addressed_user_message(unchanged)
    assert not hasattr(unchanged, WAKE_MESSAGE_CONTEXT_ATTR)


@pytest.mark.asyncio
async def test_custom_request_and_later_event_rewrites_are_not_overwritten(plugin):
    event = await wake(plugin, "星缘呢")
    await plugin.preserve_addressed_user_message(event)
    req = ProviderRequest(prompt="别的插件生成的请求，里面也有呢")
    await plugin.restore_addressed_user_request(event, req)
    assert req.prompt == "别的插件生成的请求，里面也有呢"
    event.message_str = "别的插件改写的正文"
    req.prompt = event.message_str
    await plugin.restore_addressed_user_request(event, req)
    assert req.prompt == event.message_str


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_by", ["disabled", "duplicate", "scope", "synthetic", "missing_source", "mismatch"])
async def test_only_enabled_in_scope_real_wake_changes_are_restored(plugin, blocked_by):
    event = await wake(plugin, "星缘呢")
    if blocked_by == "disabled":
        plugin.enabled = False
    elif blocked_by == "duplicate":
        plugin._private_companion_duplicate_instance = True
    elif blocked_by == "scope":
        plugin._bot_scope_allows_event.return_value = False
    elif blocked_by == "synthetic":
        event.private_companion_proactive_framework = True
    elif blocked_by == "missing_source":
        event.message_obj.message_str = None
    else:
        event.message_str = "别的文本"
    req = ProviderRequest(prompt=event.message_str)
    await plugin.preserve_addressed_user_message(event)
    await plugin.restore_addressed_user_request(event, req)
    assert req.prompt == event.message_str
    assert not hasattr(event, WAKE_MESSAGE_CONTEXT_ATTR)


def test_request_hook_runs_before_memory_and_state_enrichment():
    hooks = star_handlers_registry.get_handlers_by_event_type(EventType.OnLLMRequestEvent)
    by_name = {hook.handler_name: hook for hook in hooks}
    restore = by_name["restore_addressed_user_request"]
    assert restore.handler_module_path == "astrbot_plugin_private_companion.main"
    assert restore.extras_configs["priority"] > by_name["inject_humanized_state"].extras_configs.get("priority", 0)
    assert restore.extras_configs["priority"] > -20


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
async def test_real_memory_resolver_uses_full_text_and_rejects_stale_snapshot(plugin, memory_plugin_root, private):
    package_name = "wake_context_memory_integration"
    package = types.ModuleType(package_name)
    package.__path__ = [str(memory_plugin_root)]
    sys.modules[package_name] = package
    identity = importlib.import_module(f"{package_name}.core.identity").IdentityResolver()
    event = await wake(plugin, "星缘呢", private=private)
    await plugin.preserve_addressed_user_message(event)
    req = ProviderRequest(prompt=event.message_str)
    await plugin.restore_addressed_user_request(event, req)
    context = await identity.resolve_event_context(event)
    assert context.message_text == req.prompt == "星缘呢"
    assert context.scope == ("private" if private else "group")
    assert event.get_message_str() == "呢"
    event.message_str = "后续插件改写"
    assert (await identity.resolve_event_context(event)).message_text == "后续插件改写"

    command = await wake(plugin, "星缘订阅列表", private=private)
    await plugin.preserve_addressed_user_message(command)
    assert (await identity.resolve_event_context(command)).message_text == "订阅列表"
