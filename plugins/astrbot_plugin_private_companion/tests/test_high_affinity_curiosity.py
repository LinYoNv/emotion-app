# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.companion_interaction_expression import (
    build_expression_decision,
    expression_decision_prompt,
)
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class _ProactiveCuriosityHarness(ProactiveMessageMixin):
    @staticmethod
    def _relationship_profile(user: dict) -> dict:
        return {"stage_key": user.get("stage_key", "acquaintance")}


class HighAffinityCuriosityTests(unittest.TestCase):
    @staticmethod
    def _decision(score: int, stage_key: str, *, energy: int = 70):
        return build_expression_decision(
            {
                "relationship_role": "friend",
                "relationship_score": score,
                "relationship_baseline": {
                    "stage_key": stage_key,
                    "proactive_care_limit": 2,
                    "soft_behaviors": {"allow_followup": True},
                },
                "current_interaction": {"expression_band": "warm"},
                "bot_state": {"energy": energy},
                "proactive_candidate": {"eligible": True, "budget": 2},
            }
        )

    def test_close_relationship_adds_relational_curiosity_without_request(self) -> None:
        decision = self._decision(700, "close")

        self.assertIn("relational_curiosity", decision.allowed_behaviors)
        self.assertNotIn("reciprocal_request", decision.allowed_behaviors)
        prompt = expression_decision_prompt(decision)
        self.assertIn("彼此相处感受", prompt)
        self.assertIn("不必每轮都问", prompt)

    def test_intimate_relationship_can_make_a_refusable_light_request(self) -> None:
        decision = self._decision(950, "intimate")

        self.assertIn("relational_curiosity", decision.allowed_behaviors)
        self.assertIn("reciprocal_request", decision.allowed_behaviors)
        prompt = expression_decision_prompt(decision)
        self.assertIn("容易拒绝的低负担小忙", prompt)
        self.assertIn("拍一张特定但不敏感的生活照片", prompt)
        self.assertIn("不索取表态、承诺、秘密、排他性或即时回复", prompt)

    def test_low_energy_suppresses_high_affinity_followup_affordances(self) -> None:
        decision = self._decision(950, "intimate", energy=15)

        self.assertFalse(decision.followup)
        self.assertNotIn("relational_curiosity", decision.allowed_behaviors)
        self.assertNotIn("reciprocal_request", decision.allowed_behaviors)

    def test_proactive_hint_scales_with_stage_and_respects_pressure_gates(self) -> None:
        harness = _ProactiveCuriosityHarness()

        close_hint = harness._format_proactive_relationship_initiative_hint(
            {"stage_key": "close", "ignored_streak": 0},
            reason="check_in",
            action="message",
        )
        intimate_hint = harness._format_proactive_relationship_initiative_hint(
            {"stage_key": "intimate", "ignored_streak": 0},
            reason="check_in",
            action="message",
        )

        self.assertIn("高亲密关系主动性", close_hint)
        self.assertIn("对彼此相处的感受", close_hint)
        self.assertNotIn("小请求", close_hint)
        self.assertIn("低负担", intimate_hint)
        self.assertIn("指定主题但不敏感的生活照片", intimate_hint)
        self.assertIn("门牌住址、实时定位或他人隐私", intimate_hint)
        self.assertEqual(
            "",
            harness._format_proactive_relationship_initiative_hint(
                {"stage_key": "intimate", "ignored_streak": 1},
                reason="check_in",
                action="message",
            ),
        )
        self.assertEqual(
            "",
            harness._format_proactive_relationship_initiative_hint(
                {"stage_key": "intimate", "ignored_streak": 0},
                reason="news_share",
                action="message",
            ),
        )
        self.assertEqual(
            "",
            harness._format_proactive_relationship_initiative_hint(
                {"stage_key": "intimate", "ignored_streak": 0},
                reason="check_in",
                action="photo_text",
            ),
        )


if __name__ == "__main__":
    unittest.main()
