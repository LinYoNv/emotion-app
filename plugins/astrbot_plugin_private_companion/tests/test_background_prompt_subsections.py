# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect

from astrbot_plugin_private_companion import creative, dreaming, planning, user_memory


def test_migrated_background_prompt_producers_do_not_hand_write_legacy_titles() -> None:
    producers = (
        creative.CreativeMixin._generate_creative_project,
        creative.CreativeMixin._generate_creative_chunk,
        dreaming.generate_enhanced_dream_pick,
        dreaming.generate_daily_diary,
        planning.generate_detail_enhancement,
        planning.generate_daily_plan,
        planning._build_schedule_reference_sections,
        planning._relationship_authority_guard,
        planning._build_maslow_schedule_influence_prompt,
        planning.build_daily_plan_prompt_section,
        planning.build_detail_enhancement_prompt_section,
        user_memory.UserMemoryMixin._decide_smart_silence,
        user_memory.UserMemoryMixin._review_and_rewrite_response,
        user_memory.UserMemoryMixin._maybe_refresh_dialogue_episode,
        user_memory.UserMemoryMixin._maybe_refresh_companion_memory,
    )

    for producer in producers:
        source = inspect.getsource(producer)
        assert "【" not in source, producer.__qualname__


def test_planning_cache_parser_retains_legacy_marker_literal() -> None:
    source = inspect.getsource(planning.split_detail_prompt_cache_sections)

    assert 'marker = "【A｜当前段硬框架】"' in source


def test_background_subsection_adapters_accept_typed_sections() -> None:
    helpers = (
        creative._render_creative_labeled_section,
        user_memory._render_user_memory_labeled_section,
    )

    for helper in helpers:
        source = inspect.getsource(helper)
        assert "PromptRenderMode.LABELED_BLOCK" in source
        assert "prompt_section(" not in source


def test_planning_and_dreaming_construct_labeled_sections_at_production_sites() -> None:
    assert not hasattr(planning, "_render_planning_prompt_block")
    assert not hasattr(dreaming, "_render_dreaming_prompt_block")

    producers = (
        dreaming.generate_enhanced_dream_pick,
        dreaming.generate_daily_diary,
        planning.generate_detail_enhancement,
        planning.generate_daily_plan,
        planning._build_schedule_reference_sections,
        planning._relationship_authority_guard,
        planning._build_maslow_schedule_influence_prompt,
        planning.build_daily_plan_prompt_section,
        planning.build_detail_enhancement_prompt_section,
    )
    for producer in producers:
        source = inspect.getsource(producer)
        assert "prompt_section(" in source, producer.__qualname__
        assert "PromptRenderMode.LABELED_BLOCK" in source, producer.__qualname__
