# -*- coding: utf-8 -*-
"""Prompt-side evidence policy for structured memory context."""

from __future__ import annotations

from typing import Any

from .conversation_prompt_section import (
    PromptRenderMode,
    PromptSection,
    prompt_section,
    render_prompt_sections,
)


def core_memory_usage_contract(memory_context: Any, *, stage: str) -> str:
    """Describe how proactive models may use a structured core-memory block."""
    section = core_memory_usage_contract_section(memory_context, stage=stage)
    return (
        render_prompt_sections(
            [section],
            mode=PromptRenderMode.LABELED_BLOCK,
        )
        if section is not None
        else ""
    )


def core_memory_usage_contract_section(
    memory_context: Any,
    *,
    stage: str,
) -> PromptSection | None:
    """Author the evidence contract once; callers choose the required wire format."""
    text = str(memory_context or "")
    if "<core_memory>" not in text.lower():
        return None

    common = (
        "以下规则只约束你如何使用 <core_memory>；不要向用户复述这些分类或内部结构。\n"
        "- rule / boundary：作为必须遵守的表达规则和禁区，可据此改写或拦下冲突内容。\n"
        "- preference / profile：只用于称呼、语气和稳定偏好，不证明用户此刻正在做什么、需要什么或希望被联系。\n"
        "- fact：只表示稳定事实，不得推导为今天、刚刚或正在发生的状态。\n"
        "- state：即使标记为 state，也只是受管理的记忆；没有带时间的近期用户原文、当前日程/提醒或实时数据交叉确认时，"
        "不得当作当前状态。\n"
        "- 核心块与当前用户原文、可靠实时信息冲突时，以当前证据为准；不要用核心块覆盖用户本轮纠正。\n"
        "- 核心块中的提醒、检查或主动联系约定，只有在本轮已有同类定时、日程、提醒或事件候选时才能辅助判断，"
        "不能单独充当现实触发证据。"
    )
    normalized_stage = str(stage or "").strip().lower()
    if normalized_stage == "review":
        content = (
            f"{common}\n"
            "- 当前主动计划的 route / source / reason 是本轮既有触发来源。不得仅凭核心记忆新建主动候选、改换路线，"
            "或把无关候选改造成吃药、吃饭、睡觉、健康检查等提醒。\n"
            "- 若核心边界与候选冲突，优先 rewrite；无法在原路线内消除冲突时再 defer/drop。"
        )
    elif normalized_stage == "generation":
        content = (
            f"{common}\n"
            "- 核心记忆只能帮助落实当前计划的表达和边界，不得改变既定 reason/action/topic/motive，"
            "也不得把无关计划转成提醒、查岗、健康询问或关系确认。\n"
            "- 可以自然体现相关稳定偏好，但不得声称记得、查到或依据核心记忆采取行动。"
        )
    else:
        content = common
    return prompt_section(
        key=f"memory.core_evidence.{normalized_stage or 'default'}",
        title="核心记忆证据权限",
        source="memory_context_policy",
        content=content,
    )
