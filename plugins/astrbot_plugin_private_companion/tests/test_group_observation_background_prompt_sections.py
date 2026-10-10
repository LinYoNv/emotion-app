# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import inspect

from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptDocument,
    PromptRenderMode,
    render_prompt_document,
)
from astrbot_plugin_private_companion.group_observation import (
    GroupObservationMixin,
    build_group_episode_cache_prompt_document,
    build_group_episode_cache_prompts,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _InterjectionPromptHarness(GroupObservationMixin):
    @staticmethod
    def _format_group_context_for_prompt_body(_group) -> str:
        return "群上下文文本"

    @staticmethod
    def _format_persona_voice_channel_prompt(_channel) -> str:
        return "【人格标准化：主动表达】\n主动风格文本"


def test_group_episode_prompt_document_preserves_both_wire_variants() -> None:
    expected = {
        False: (
            "faf7f01db68acc5a8a1f10ed68aa37a06a1588c2b5a71cf7cdc6d889d9715fbe",
            "7c5fbdfcba404cfc3817aa8d857a64c787458583612861c9884a4a7122d65a9f",
        ),
        True: (
            "faf7f01db68acc5a8a1f10ed68aa37a06a1588c2b5a71cf7cdc6d889d9715fbe",
            "9f125d38ee68207bd7d80b1aade495fa98a53b4e73b39f5c9cffaab3ff946588",
        ),
    }

    for learn_expression_rules, expected_hashes in expected.items():
        document = build_group_episode_cache_prompt_document(
            ["甲: 第一条", "乙: 第二条"],
            learn_expression_rules=learn_expression_rules,
            candidate_count=2,
            existing_rule_reference="已有规则 A",
        )
        rendered = render_prompt_document(document, mode=PromptRenderMode.BODY_ONLY)
        legacy_system, legacy_user = build_group_episode_cache_prompts(
            ["甲: 第一条", "乙: 第二条"],
            learn_expression_rules=learn_expression_rules,
            candidate_count=2,
            existing_rule_reference="已有规则 A",
        )

        assert isinstance(document, PromptDocument)
        assert document.system[0].key == "background.group_episode.system"
        assert document.user[0].key == "background.group_episode.user"
        assert (rendered["system"], rendered["user"]) == (
            legacy_system,
            legacy_user,
        )
        assert (_sha256(legacy_system), _sha256(legacy_user)) == expected_hashes


def test_group_interjection_document_preserves_json_decision_wire() -> None:
    harness = _InterjectionPromptHarness()
    document = harness._group_interjection_prompt_document(
        {"group_id": "g"},
        "触发内容",
        memory_context="长期参考文本",
    )
    rendered = render_prompt_document(document, mode=PromptRenderMode.BODY_ONLY)

    assert isinstance(document, PromptDocument)
    assert document.system == ()
    assert document.user[0].key == "background.group_interject"
    assert len(rendered["user"]) == 658
    assert _sha256(rendered["user"]) == (
        "f5ed15ed237b718015bc06917cadce9fe4b2e5cd244c8a2cd632da7acda52afd"
    )
    assert rendered["user"].endswith(
        '{"should_reply":false,"text":"","reason":"不超过12字"}'
    )


def test_group_slang_document_preserves_optional_web_evidence_spacing() -> None:
    harness = GroupObservationMixin()
    cases = (
        (
            "外部证据文本",
            677,
            "d4a78c5165500e2b84f87f3ee2550a0d839099cfba45bcf35d4da9f46e5576c8",
            "外部证据文本\n\n\n只输出 JSON",
        ),
        (
            "",
            575,
            "edd53b4abb8d5f57db25ed469cdc8c58bf085da9df3eb672bbef11711d19469b",
            "甲: 词甲 内容\n\n\n\n只输出 JSON",
        ),
    )

    for web_evidence, expected_length, expected_hash, boundary in cases:
        document = harness._group_slang_prompt_document(
            ["词甲", "词乙", "词丙"],
            ["甲: 词甲 内容"],
            web_evidence=web_evidence,
        )
        rendered = render_prompt_document(
            document,
            mode=PromptRenderMode.BODY_ONLY,
        )["user"]

        assert isinstance(document, PromptDocument)
        assert document.user[0].key == "background.group_slang"
        assert len(rendered) == expected_length
        assert _sha256(rendered) == expected_hash
        assert boundary in rendered


def test_background_prompt_producers_do_not_hand_write_legacy_headings() -> None:
    producers = (
        build_group_episode_cache_prompt_document,
        GroupObservationMixin._group_interjection_prompt_document,
        GroupObservationMixin._group_slang_prompt_document,
    )

    for producer in producers:
        source = inspect.getsource(producer)
        assert "【" not in source
        assert "prompt_section(" in source
