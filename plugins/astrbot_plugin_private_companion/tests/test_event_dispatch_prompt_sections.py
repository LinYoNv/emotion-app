# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptRenderMode,
    prompt_section,
    prompt_text,
    render_prompt_sections,
)
from astrbot_plugin_private_companion.event_dispatch import EventDispatchMixin


class _PromptDiagnosticsHarness(EventDispatchMixin):
    def __init__(self) -> None:
        self.data: dict = {}
        self._data_lock = asyncio.Lock()

    @staticmethod
    def _format_timestamp_elapsed(_value) -> str:
        return "刚刚"

    @staticmethod
    def _schedule_data_save(**_kwargs) -> None:
        return None


class _RecallPromptHarness(EventDispatchMixin):
    enable_recall_enhancement = True
    enable_recall_transcribe_command = True

    @staticmethod
    def _can_manage_private_companion(_event) -> bool:
        return True

    @staticmethod
    def _can_manage_group_companion(_event) -> bool:
        return True

    @staticmethod
    def _event_scope_key(_event) -> str:
        return "group:10001"

    @staticmethod
    def _recent_recalled_messages_for_scope(_scope: str, *, limit: int = 5):
        assert limit == 5
        return [
            {
                "sender_name": "Alice",
                "sender_id": "QQ:123",
                "text": "正文中保留【用户原文】",
                "recalled_ts": 100,
            }
        ]

    @staticmethod
    def _recall_image_status_summary(_row) -> str:
        return ""

    @staticmethod
    def _format_timestamp_elapsed(_value) -> str:
        return "1 分钟前"


def test_recall_query_is_authored_as_a_canonical_section() -> None:
    harness = _RecallPromptHarness()
    event = SimpleNamespace(is_private_chat=lambda: False)

    section = harness._format_recalled_messages_for_natural_query_prompt_section(event)

    assert section.key == "recall.query"
    assert section.title == "撤回消息查询"
    assert section.source == "event_dispatch"
    assert "【撤回消息查询】" not in section.content
    assert "正文中保留【用户原文】" in section.content
    assert '<section title="撤回消息查询">' in render_prompt_sections(
        [section],
        mode=PromptRenderMode.CONVERSATION_XML,
    )


def test_recall_query_legacy_wrapper_and_section_preserve_wire() -> None:
    harness = _RecallPromptHarness()
    event = SimpleNamespace(is_private_chat=lambda: False)

    with_heading = harness._format_recalled_messages_for_natural_query(event)
    body_only = render_prompt_sections(
        [harness._format_recalled_messages_for_natural_query_prompt_section(event)],
        mode=PromptRenderMode.BODY_ONLY,
    )

    assert with_heading == f"【撤回消息查询】\n{body_only}"
    assert body_only.startswith("用户正在问当前会话刚才撤回了什么。")
    assert "【撤回消息查询】" not in body_only


def test_prompt_diagnostics_prefers_typed_section_manifest() -> None:
    harness = _PromptDiagnosticsHarness()
    section = prompt_section(
        key="identity.anchor",
        title="身份锚点",
        source="identity",
        content=prompt_text("第一行", "正文中的【不是模块标题】", separator="\n"),
        metadata={"范围": "当前会话"},
    )

    modules = harness._normalize_prompt_injection_modules(
        "【伪标题】\n不应参与模块识别",
        [section],
        legacy_heading_fallback=False,
    )

    assert len(modules) == 1
    assert modules[0]["key"] == "identity.anchor"
    assert modules[0]["title"] == "身份锚点"
    assert modules[0]["source"] == "identity"
    assert modules[0]["content"] == "第一行\n正文中的【不是模块标题】"
    assert modules[0]["metadata"] == {"范围": "当前会话"}


def test_prompt_diagnostics_reads_persisted_manifest_text_without_coercion() -> None:
    harness = _PromptDiagnosticsHarness()
    manifest = [
        {
            "key": "state.full",
            "title": "完整模拟状态",
            "source": "daily_state",
            "priority": 37,
            "content": "状态一\n状态二",
            "chars": 7,
        }
    ]

    modules = harness._normalize_prompt_injection_modules(
        "不会用于反解析",
        manifest,
        legacy_heading_fallback=False,
    )

    assert modules[0]["priority"] == 37
    assert modules[0]["content"] == "状态一\n状态二"
    assert modules[0]["chars"] == 7


def test_prompt_diagnostics_ignores_non_text_mapping_content() -> None:
    harness = _PromptDiagnosticsHarness()
    manifest = [
        {
            "key": "state.full",
            "title": "完整模拟状态",
            "source": "daily_state",
            "content": prompt_text("状态一", "状态二", separator="\n"),
        }
    ]

    modules = harness._normalize_prompt_injection_modules(
        "不会用于反解析",
        manifest,
        legacy_heading_fallback=False,
    )

    assert modules == []


@pytest.mark.asyncio
async def test_live_trace_without_manifest_does_not_infer_heading_modules() -> None:
    harness = _PromptDiagnosticsHarness()
    text = "【用户原文中的标题】\n这只是正文"

    await harness._record_prompt_injection_snapshot(
        kind="request",
        session="test:FriendMessage:1",
        title="测试注入",
        text=text,
    )

    modules = harness.data["recent_prompt_injections"]["request"][0]["modules"]
    assert [module["key"] for module in modules] == ["prompt.full"]
    assert modules[0]["content"] == text


@pytest.mark.asyncio
async def test_live_trace_with_empty_manifest_still_uses_full_prompt_module() -> None:
    harness = _PromptDiagnosticsHarness()
    text = "【用户原文中的标题】\n这只是正文"

    await harness._record_prompt_injection_snapshot(
        kind="passive",
        session="test:FriendMessage:1",
        title="测试注入",
        text=text,
        section_manifest=[],
    )

    modules = harness.data["recent_prompt_injections"]["passive"][0]["modules"]
    assert [module["key"] for module in modules] == ["prompt.full"]
    assert modules[0]["content"] == text


@pytest.mark.asyncio
async def test_live_trace_prefers_section_manifest_over_legacy_modules() -> None:
    harness = _PromptDiagnosticsHarness()
    section = prompt_section(
        key="recall.query",
        title="撤回消息查询",
        source="event_dispatch",
        content="typed body",
    )

    await harness._record_prompt_injection_snapshot(
        kind="request",
        session="test:FriendMessage:1",
        title="测试注入",
        text="rendered prompt",
        modules=[{"key": "legacy", "content": "legacy body"}],
        section_manifest=[section],
    )

    modules = harness.data["recent_prompt_injections"]["request"][0]["modules"]
    assert [module["key"] for module in modules] == ["recall.query"]
    assert modules[0]["title"] == "撤回消息查询"
    assert modules[0]["source"] == "event_dispatch"
    assert modules[0]["content"] == "typed body"


def test_historical_trace_without_manifest_keeps_heading_recovery() -> None:
    harness = _PromptDiagnosticsHarness()
    text = "引言\n\n【旧模块】\n旧正文"

    modules = harness._normalize_prompt_injection_modules(text, None)

    assert [module["key"] for module in modules] == [
        "section.0.intro",
        "section.1.旧模块",
    ]
    assert modules[1]["source"] == "prompt_heading_split"
    assert modules[1]["content"] == "【旧模块】\n旧正文"
