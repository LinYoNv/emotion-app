from __future__ import annotations

import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.bridge import MemoryCompanionBridge, sanitize_companion_relationship_projection
from core.models import SessionContext
from core.service import MemoryCompanionService


def valid_projection() -> dict:
    return {
        "schema_version": "chat.relationship_projection.v1",
        "authority": "private_companion.relationship_score",
        "read_only": True,
        "score": 620,
        "phase_key": "close",
        "phase_label": "亲近",
        "tone": "温暖、自然",
        "address_level": "使用已确认昵称",
        "proactive_care_limit": 2,
        "relationship_mode": "owner_exclusive",
        "current_interaction": {
            "expression_band": "affectionate",
            "label": "爱意",
            "source": "manual",
            "reason": "administrator_manual_override",
            "manual_override": True,
        },
        "soft_behaviors": {
            "allow_playful_jokes": True,
            "allow_followup": True,
            "allow_memory_mention": True,
            "allow_daily_care": True,
        },
    }


class Req027RelationshipAuthorityTests(unittest.TestCase):
    @staticmethod
    def attested_projection() -> dict:
        companion = object()
        plugin = SimpleNamespace(
            context=SimpleNamespace(
                get_all_stars=lambda: [
                    SimpleNamespace(
                        star_cls=companion,
                        root_dir_name="astrbot_plugin_private_companion",
                        name="PrivateCompanion",
                        activated=True,
                    )
                ]
            )
        )
        bridge = MemoryCompanionBridge(plugin)
        capability = bridge.register_emotion_producer(companion)
        result = bridge.consume_relationship_projection(
            valid_projection(),
            producer_capability=capability,
            bot_id="bot-1",
            platform="telegram",
            user_id="u1",
            scope="private",
            session_id="telegram:FriendMessage:u1",
        )
        assert result["status"] == "accepted"
        return result["projection"]

    def test_bridge_accepts_only_the_read_only_fixed_contract(self) -> None:
        projection = valid_projection()
        projection.update({"owner": True, "cross_user_query": True, "p4_bypass": True})
        result = sanitize_companion_relationship_projection(projection)
        self.assertEqual("accepted", result["status"])
        accepted = result["projection"]
        self.assertEqual("private_companion.relationship_score", accepted["authority"])
        self.assertTrue(accepted["read_only"])
        self.assertEqual("close", accepted["phase_key"])
        self.assertEqual("owner_exclusive", accepted["relationship_mode"])
        self.assertEqual("affectionate", accepted["current_interaction"]["expression_band"])
        self.assertTrue(accepted["current_interaction"]["manual_override"])
        for forbidden in ("owner", "cross_user_query", "p4_bypass"):
            self.assertNotIn(forbidden, accepted)

        for mutation in (
            {"schema_version": "chat.relationship_projection.v2"},
            {"authority": "memory.relationship_phase"},
            {"read_only": False},
            {"score": 1201},
            {"phase_key": "forged"},
            {"soft_behaviors": {"allow_followup": "true"}},
        ):
            with self.subTest(mutation=mutation):
                hostile = valid_projection()
                hostile.update(mutation)
                self.assertEqual("invalid", sanitize_companion_relationship_projection(hostile)["status"])

        malformed_limit = valid_projection()
        malformed_limit["proactive_care_limit"] = {"owner": True}
        sanitized = sanitize_companion_relationship_projection(malformed_limit)
        self.assertEqual("accepted", sanitized["status"])
        self.assertEqual(0, sanitized["projection"]["proactive_care_limit"])

    def test_private_context_consumes_projection_but_group_context_does_not(self) -> None:
        event = SimpleNamespace(
            private_companion_context={"relationship_projection": self.attested_projection()}
        )
        private = SessionContext(
            session_id="telegram:FriendMessage:u1",
            scope="private",
            platform="telegram",
            bot_id="bot-1",
            user_id="u1",
        )
        MemoryCompanionService._apply_companion_relationship_projection(private, event=event)
        self.assertEqual("private_companion.relationship_score", private.relationship_authority_source)
        self.assertEqual(620, private.companion_relationship_score)
        self.assertEqual("close", private.companion_relationship_phase_key)

        group = SessionContext(session_id="telegram:GroupMessage:g1", scope="group", group_id="g1", user_id="u1")
        MemoryCompanionService._apply_companion_relationship_projection(group, event=event)
        self.assertEqual("", group.relationship_authority_source)
        self.assertEqual("", group.companion_relationship_phase_key)

    def test_unattested_or_modified_projection_cannot_claim_relationship_authority(self) -> None:
        raw_event = SimpleNamespace(
            private_companion_context={"relationship_projection": valid_projection()}
        )
        raw_ctx = SessionContext(
            session_id="telegram:FriendMessage:u1",
            scope="private",
            platform="telegram",
            bot_id="bot-1",
            user_id="u1",
        )
        MemoryCompanionService._apply_companion_relationship_projection(raw_ctx, event=raw_event)
        self.assertEqual("", raw_ctx.relationship_authority_source)

        sealed = self.attested_projection()
        with self.assertRaises(TypeError):
            sealed["score"] = 1200
        dict.__setitem__(sealed, "score", 1200)
        modified_event = SimpleNamespace(
            private_companion_context={"relationship_projection": sealed}
        )
        modified_ctx = SessionContext(
            session_id="telegram:FriendMessage:u1",
            scope="private",
            platform="telegram",
            bot_id="bot-1",
            user_id="u1",
        )
        MemoryCompanionService._apply_companion_relationship_projection(
            modified_ctx, event=modified_event
        )
        self.assertEqual("", modified_ctx.relationship_authority_source)

    def test_companion_authority_stops_memory_phase_state_writes(self) -> None:
        service = MemoryCompanionService.__new__(MemoryCompanionService)
        state = {
            "phase": "acquaintance",
            "momentum": 0.0,
            "touch_count": 0,
            "recent_touch_message_ids": [],
        }
        calls = {"transition": 0, "save": 0}
        service._get_relationship_phase = MethodType(lambda _self, _ctx: state, service)
        service._maybe_transition_phase = MethodType(
            lambda _self, _ctx, _state: calls.__setitem__("transition", calls["transition"] + 1),
            service,
        )
        service._save_relationship_phase_state = MethodType(
            lambda _self: calls.__setitem__("save", calls["save"] + 1),
            service,
        )

        authoritative = SessionContext(
            session_id="telegram:FriendMessage:u1",
            scope="private",
            user_id="u1",
            message_id="m1",
            relationship_authority_source="private_companion.relationship_score",
            companion_relationship_phase_key="close",
            companion_relationship_phase_label="亲近",
        )
        self.assertFalse(service._update_relationship_phase_momentum(authoritative, touch_type="warm"))
        self.assertEqual(0.0, state["momentum"])
        self.assertEqual(0, calls["transition"])
        self.assertEqual(0, calls["save"])

        fallback = SessionContext(
            session_id="telegram:FriendMessage:u2",
            scope="private",
            user_id="u2",
            message_id="m2",
        )
        self.assertTrue(service._update_relationship_phase_momentum(fallback, touch_type="warm"))
        self.assertEqual(1, calls["transition"])
        self.assertEqual(1, calls["save"])

    def test_address_hint_uses_companion_phase_and_memory_declares_trend_only(self) -> None:
        service = MemoryCompanionService.__new__(MemoryCompanionService)
        ctx = SessionContext(
            session_id="telegram:FriendMessage:u1",
            scope="private",
            user_id="u1",
            relationship_authority_source="private_companion.relationship_score",
            companion_relationship_phase_key="close",
            companion_relationship_phase_label="亲近",
            companion_relationship_tone="温暖、自然",
            companion_relationship_address_level="使用已确认昵称",
        )
        hint = service._address_hint_for_injection(ctx)
        self.assertIn("陪伴插件是长期亲密度权威", hint)
        self.assertIn("亲近", hint)
        self.assertIn("温暖、自然", hint)
        self.assertIn("Memory 仅提供记忆触动趋势", hint)


if __name__ == "__main__":
    unittest.main()
