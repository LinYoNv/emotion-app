from __future__ import annotations

from datetime import datetime
import inspect

from astrbot_plugin_private_companion import passive_state_pipeline
from astrbot_plugin_private_companion.daily_state import DailyStateMixin


class _PromptSectionHarness(DailyStateMixin):
    enable_llm_timer_scheduling = True
    enable_skill_growth_simulation = True

    def __init__(self) -> None:
        self.data = {
            "daily_state": {},
            "daily_plan": {},
            "daily_story_plan": {},
            "detail_enhanced_segments": {},
            "skill_growth": {
                "skills": {
                    "writing": {
                        "name": "写作",
                        "level": 4,
                        "exp": 700,
                        "hidden": False,
                    }
                }
            },
        }

    @staticmethod
    def _private_user_role(_user=None, *_args) -> str:
        return "owner"

    @staticmethod
    def _activity_followup_quota_policy(_user) -> dict:
        return {
            "tier": 3,
            "tier_label": "L3",
            "max_intensity": 2,
            "completion_buffer_minutes": 5,
            "generation_rule": "允许自然回访。",
        }

    @staticmethod
    def _environment_fromtimestamp(value: float) -> datetime:
        return datetime.fromtimestamp(value)

    @staticmethod
    def _reality_touch_audio_consented(_user) -> bool:
        return False

    @staticmethod
    def _format_schedule_context_for_prompt() -> str:
        return "当前在家休息。"

    @staticmethod
    def _format_story_plan_for_prompt() -> str:
        return "（暂无）"

    @staticmethod
    def _format_important_dates_for_prompt() -> str:
        return "明天是纪念日。"


def _assert_section(section, *, key: str, title: str) -> None:
    assert section.key == key
    assert section.title == title
    assert section.source == "daily_state"
    assert f"【{title}】" not in section.content


def test_life_and_important_date_sections_own_their_metadata() -> None:
    harness = _PromptSectionHarness()

    _assert_section(
        harness._format_life_context_prompt_section(),
        key="life.context",
        title="Bot 模拟生活背景",
    )
    _assert_section(
        harness._format_important_dates_prompt_section(),
        key="important.dates",
        title="近期重要日期",
    )


def test_detail_and_timer_sections_keep_protocol_text_in_the_body() -> None:
    harness = _PromptSectionHarness()

    detail = harness._format_detail_injection_prompt_section()
    _assert_section(detail, key="detail.injection", title="Bot 模拟当前片段")
    timer = harness._format_timer_scheduling_prompt_section(
        {"relationship_role": "owner"},
    )
    _assert_section(timer, key="timer.scheduling", title="临时预约与动作回访")
    assert "<timer>" in timer.content
    assert timer.content.startswith("当前本地时间：")


def test_skill_sections_are_typed_for_full_and_matched_views() -> None:
    harness = _PromptSectionHarness()

    _assert_section(
        harness._format_skill_growth_prompt_section(),
        key="skill.growth",
        title="能力熟悉度",
    )
    _assert_section(
        harness._format_skill_growth_for_user_text_prompt_section("聊聊写作"),
        key="skill.growth_match",
        title="本轮相关技能",
    )


def test_daily_state_and_passive_prompt_interfaces_have_no_dual_output_controls() -> None:
    removed_wrappers = (
        "_format_meal_care_reply_context",
        "_format_food_menu_for_reply",
        "_format_dialogue_outfit_continuity_for_prompt",
        "_format_active_period_boundary_for_prompt",
        "_format_recent_creative_share_snapshot_for_reply",
        "_format_recent_photo_share_snapshot_for_reply",
        "_format_hidden_creative_context_for_reply",
        "_format_skill_growth_for_prompt",
        "_format_skill_growth_for_user_text",
        "_format_timer_scheduling_instruction",
        "_prepared_lightweight_state_injection",
    )
    for name in removed_wrappers:
        assert not hasattr(DailyStateMixin, name), name

    for name in (
        "_format_state_for_prompt",
        "_format_state_injection",
        "_format_life_context_injection",
        "_format_important_dates_injection",
        "_format_memo_notes_for_prompt",
        "_format_detail_injection",
    ):
        parameters = inspect.signature(getattr(DailyStateMixin, name)).parameters
        assert "include_heading" not in parameters
        assert "as_section" not in parameters
        assert "as_sections" not in parameters

    passive_source = inspect.getsource(passive_state_pipeline.inject_humanized_state)
    assert "include_heading" not in passive_source
    assert "as_section=" not in passive_source
    assert "as_sections=" not in passive_source
