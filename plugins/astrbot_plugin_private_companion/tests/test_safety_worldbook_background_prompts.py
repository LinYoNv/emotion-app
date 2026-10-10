# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import inspect

from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptDocument,
    PromptRenderMode,
    render_prompt_document,
)
from astrbot_plugin_private_companion.group_member_safety import GroupMemberSafetyMixin
from astrbot_plugin_private_companion.worldbook import WorldbookMixin


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _SafetyPromptHarness(GroupMemberSafetyMixin):
    @staticmethod
    def _group_member_safety_relation_role(_user_id) -> str:
        return "普通群成员"

    @staticmethod
    def _group_member_safety_targeted_flow(_group, *, max_lines=10) -> str:
        assert max_lines == 10
        return "- message_id=m1 sender=user-1 target target target_TO=bot"


class _WorldbookPromptHarness(WorldbookMixin):
    @staticmethod
    def _get_default_persona_prompt() -> str:
        return "人格文本"


def test_group_member_safety_json_prompt_uses_body_only_document() -> None:
    harness = _SafetyPromptHarness()
    document = harness._group_member_safety_judge_prompt_document(
        {},
        sender_id="user-1",
        sender_name="测试群友",
        text="当前消息",
        scene={"talking_to": "bot", "trigger": "at_bot"},
        recent_flow="测试群友：当前消息",
    )
    rendered = render_prompt_document(
        document,
        mode=PromptRenderMode.BODY_ONLY,
    )["user"]

    assert isinstance(document, PromptDocument)
    assert document.system == ()
    assert document.user[0].key == "background.group_member_safety"
    assert len(rendered) == 1957
    assert _sha256(rendered) == (
        "222798f58f9b4c586b47542513393a55872ca3a5a884894b9574bf83d2e777ed"
    )
    assert '"malicious": false' in rendered
    assert "```" not in rendered


def test_worldbook_registration_uses_body_only_document_without_wire_change() -> None:
    harness = _WorldbookPromptHarness()
    document = harness._worldbook_self_registration_prompt_document(
        {
            "user_id": "10001",
            "group_id": "20001",
            "name": "小明",
            "aliases": ["明明"],
            "text": "我是小明",
            "recent": [{"identity_name": "群友甲", "text": "你好"}],
        }
    )
    rendered = render_prompt_document(
        document,
        mode=PromptRenderMode.BODY_ONLY,
    )["user"]

    assert isinstance(document, PromptDocument)
    assert document.system == ()
    assert document.user[0].key == "background.worldbook_registration"
    assert len(rendered) == 232
    assert _sha256(rendered) == (
        "738f0e379ccfbbcedd6dd309870b836655436d8b94897d904c85bf7b38de6c2f"
    )
    assert "【Bot 人格】\n人格文本" in rendered
    assert "【自我介绍原文】\n我是小明" in rendered


def test_background_producers_do_not_hand_write_legacy_headings() -> None:
    producers = (
        GroupMemberSafetyMixin._group_member_safety_judge_prompt_document,
        WorldbookMixin._worldbook_self_registration_prompt_document,
    )

    for producer in producers:
        source = inspect.getsource(producer)
        assert "【" not in source
        assert "prompt_section(" in source
