# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy

from astrbot_plugin_private_companion.user_memory import UserMemoryMixin


class _RelationshipHarness(UserMemoryMixin):
    def __init__(self) -> None:
        self.data = {"users": {"user-1": {"relationship_score": 3}}}
        self.llm_calls = 0

    async def _llm_call(self, *_args, **_kwargs) -> str:
        self.llm_calls += 1
        return "{}"


class _IntentHarness(UserMemoryMixin):
    enable_custom_relationship_stage_policy = True

    def __init__(self) -> None:
        self.settlement_calls = 0

    def _settle_current_interaction_from_intent(self, user, intent) -> None:
        self.settlement_calls += 1
        user["current_interaction"] = {"expression_band": intent.get("band", "neutral")}


class RelationshipAnalysisEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_relationship_refresh_is_a_fast_noop(self) -> None:
        harness = _RelationshipHarness()
        user = harness.data["users"]["user-1"]
        before = deepcopy(harness.data)

        refreshed = await asyncio.wait_for(
            harness._refresh_persona_relationship("user-1", dict(user), trigger="inbound"),
            timeout=0.1,
        )

        self.assertFalse(refreshed)
        self.assertEqual(0, harness.llm_calls)
        self.assertEqual(before, harness.data)

    async def test_force_does_not_reenable_removed_relationship_analyzer(self) -> None:
        harness = _RelationshipHarness()

        refreshed = await asyncio.wait_for(
            harness._refresh_persona_relationship(
                "user-1",
                dict(harness.data["users"]["user-1"]),
                trigger="manual",
                force=True,
            ),
            timeout=0.1,
        )

        self.assertFalse(refreshed)
        self.assertEqual(0, harness.llm_calls)

    def test_intent_updates_use_only_the_req040_projection(self) -> None:
        harness = _IntentHarness()
        user = {"relationship_state": {"mode": "legacy"}}

        harness._update_relationship_state_from_intent(user, {"band": "warm"})

        self.assertEqual(1, harness.settlement_calls)
        self.assertEqual({"expression_band": "warm"}, user["current_interaction"])
        self.assertNotIn("relationship_state", user)


if __name__ == "__main__":
    unittest.main()
