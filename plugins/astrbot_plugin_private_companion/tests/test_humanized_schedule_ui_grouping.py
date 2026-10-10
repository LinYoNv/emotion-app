# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HumanizedScheduleUiGroupingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        cls.index = (ROOT / "pages" / "陪伴面板" / "index.html").read_text(encoding="utf-8")

    def test_schedule_switches_are_embedded_in_humanized_life(self) -> None:
        self.assertIn('enable_humanized_states: ["拟人生活状态"', self.script)
        self.assertIn('enable_daily_plan: "enable_humanized_states"', self.script)
        self.assertIn('enable_detail_enhancement: "enable_humanized_states"', self.script)
        self.assertIn('enable_daily_diary: "enable_humanized_states"', self.script)

    def test_humanized_detail_contains_complete_schedule_sections(self) -> None:
        for title in ("生活日程", "日程细化", "日记与重要日期", "日程生成高级"):
            self.assertIn(f'title: "{title}"', self.script)
        for key in (
            "daily_plan_time",
            "daily_plan_item_count",
            "detail_enhancement_lead_minutes",
            "daily_diary_time",
            "max_diary_entries",
            "important_date_lookahead_days",
            "daily_plan_prompt",
        ):
            self.assertIn(key, self.script)

    def test_schedule_child_controls_follow_their_own_switches(self) -> None:
        self.assertIn('dailyPlanChildren.has(settingKey) && !boolSetting("enable_daily_plan")', self.script)
        self.assertIn('settingKey === "detail_enhancement_lead_minutes" && !boolSetting("enable_detail_enhancement")', self.script)
        self.assertIn('diaryChildren.has(settingKey) && !boolSetting("enable_daily_diary")', self.script)
        for key in ("enable_daily_plan", "enable_detail_enhancement", "enable_daily_diary", "enable_daily_greetings", "enable_enhanced_dreams"):
            self.assertIn(f'"{key}",', self.script)

    def test_detail_projection_uses_clock_phase_for_familiar_display_labels(self) -> None:
        self.assertIn("scheduleTimelineSegmentLabel(segment)", self.script)
        self.assertIn("scheduleStoryLifecycleLabel(item)", self.script)
        self.assertIn(
            'scheduleLifecycleLabel(clockStatus || (!evidence ? legacy : "") || evidence || legacy)',
            self.script,
        )
        self.assertIn('scheduleLifecycleLabel(item?.clock_status || lifecycle)', self.script)
        self.assertNotIn('active: "当前时段·计划投影"', self.script)
        self.assertNotIn('completed: "时段已过·未核实"', self.script)

    def test_legacy_dashboard_consumers_use_explicit_plan_projection_labels(self) -> None:
        self.assertIn("const currentLifecycleText = scheduleTimelineSegmentLabel(current);", self.script)
        self.assertIn("const segmentLifecycleText = scheduleTimelineSegmentLabel(segment);", self.script)
        self.assertIn("segment?.clock_status || segment?.lifecycle", self.script)
        self.assertIn('activityMeta || "当前计划时段"', self.script)
        self.assertIn('current.activity, "暂无当前计划时段"', self.script)
        self.assertIn('current.activity || "暂无当前计划时段"', self.script)
        self.assertIn("scheduleTimelineSegmentLabel(current), current.mood", self.script)
        self.assertIn('if (!evidence && !clockStatus && !legacy) return "";', self.script)
        self.assertNotIn('<em class="life-current-marker">当前日程</em>', self.script)

    def test_passive_continuity_anchor_is_grouped_after_delta_and_has_dual_visibility(
        self,
    ) -> None:
        self.assertIn(
            'enable_passive_state_continuity_anchor: ["被动状态连续性锚点"',
            self.script,
        )
        self.assertIn(
            'enable_passive_state_continuity_anchor: "enable_passive_state_delta_injection"',
            self.script,
        )
        self.assertIn(
            'enable_passive_state_delta_injection: ["enable_passive_state_continuity_anchor"]',
            self.script,
        )
        self.assertIn(
            'settingKey === "enable_passive_state_continuity_anchor"',
            self.script,
        )
        self.assertIn(
            'return boolSetting("inject_passive_states") && boolSetting("enable_passive_state_delta_injection");',
            self.script,
        )
        self.assertIn('title: "被动状态注入"', self.script)
        self.assertIn(
            'keys: ["inject_passive_states", "enable_passive_state_delta_injection", "enable_passive_state_continuity_anchor"]',
            self.script,
        )
        self.assertIn("enable_passive_state_continuity_anchor: {", self.script)

    def test_advanced_cycle_settings_are_grouped_and_conditionally_visible(self) -> None:
        for title in ("周期策略", "月经期", "卵泡期", "排卵前期", "排卵期", "黄体期", "PMS 期", "经期不适模拟"):
            self.assertIn(f'title: "{title}"', self.script)
        for key in (
            "enable_advanced_cycle_strategy",
            "advanced_cycle_link_intensity",
            "advanced_cycle_start_offset",
            "advanced_cycle_menstrual_days",
            "advanced_cycle_follicular_prompt",
            "advanced_cycle_pre_ovulation_mood",
            "advanced_cycle_ovulation_energy",
            "advanced_cycle_luteal_days",
            "advanced_cycle_pms_prompt",
        ):
            self.assertIn(key, self.script)
        self.assertIn(
            'keys: ["advanced_cycle_discomfort_simulation", "advanced_cycle_discomfort_chance", "advanced_cycle_discomfort_types"]',
            self.script,
        )
        self.assertIn('settingKey === "enable_advanced_cycle_strategy") return boolSetting("enable_cycle_state")', self.script)
        self.assertIn('!boolSetting("enable_cycle_state") || !boolSetting("enable_advanced_cycle_strategy")', self.script)
        self.assertIn('manualCycleEnergyKeys.has(settingKey) && boolSetting("advanced_cycle_link_intensity")', self.script)

    def test_cycle_discomfort_settings_grouped_right_after_pms_section(self) -> None:
        marker = 'title: "PMS 期"'
        pms_index = self.script.index(marker)
        discomfort_marker = 'title: "经期不适模拟"'
        discomfort_index = self.script.index(discomfort_marker)
        self.assertGreater(discomfort_index, pms_index)
        between = self.script[pms_index + len(marker) : discomfort_index]
        self.assertNotIn('title: "', between)

    def test_advanced_cycle_controls_rerender_without_losing_draft(self) -> None:
        for key in ("enable_cycle_state", "enable_advanced_cycle_strategy", "advanced_cycle_link_intensity"):
            self.assertIn(f'"{key}",', self.script)
        self.assertIn("preserveFeatureParamDraft();", self.script)

    def test_energy_and_mood_are_presented_as_independent_dimensions(self) -> None:
        self.assertIn('id="lifeEnergy"', self.index)
        self.assertIn('id="lifeMood"', self.index)
        self.assertIn('$("#lifeEnergy")', self.script)
        self.assertIn('$("#lifeMood")', self.script)


if __name__ == "__main__":
    unittest.main()
