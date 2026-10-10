from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.bridge import MemoryCompanionBridge, sanitize_companion_relationship_projection  # noqa: E402
from core.models import SessionContext  # noqa: E402
from core.service import MemoryCompanionService  # noqa: E402


def projection() -> dict:
    return {
        "schema_version": "chat.relationship_projection.v1",
        "authority": "private_companion.relationship_score",
        "read_only": True,
        "score": 205,
        "phase_key": "acquaintance",
        "phase_label": "初识",
        "tone": "友好",
        "address_level": "中性",
        "proactive_care_limit": 0,
        "soft_behaviors": {
            "allow_playful_jokes": False,
            "allow_followup": True,
            "allow_memory_mention": False,
            "allow_daily_care": False,
        },
        "current_interaction": {
            "expression_band": "hurt",
            "label": "受伤",
            "source": "automatic",
            "reason": "hurt_event",
            "manual_override": False,
            "dynamics_version": "interaction_dynamics.v1",
            "recovery_band": "recovering",
            "expires_at": 2000.0,
            "projection_revision": 2,
        },
    }


class EmotionE5AuthorityProjectionTests(unittest.TestCase):
    def test_memory_consumes_bounded_dynamics_without_becoming_authority(self) -> None:
        accepted = sanitize_companion_relationship_projection(projection())
        self.assertEqual("accepted", accepted["status"])
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
        sealed = bridge.consume_relationship_projection(
            projection(),
            producer_capability=capability,
            bot_id="bot-1",
            platform="qq",
            user_id="u1",
            scope="private",
            session_id="qq:FriendMessage:u1",
        )["projection"]
        ctx = SessionContext(
            scope="private",
            bot_id="bot-1",
            platform="qq",
            user_id="u1",
            session_id="qq:FriendMessage:u1",
        )
        event = SimpleNamespace(private_companion_context={"relationship_projection": sealed})
        MemoryCompanionService._apply_companion_relationship_projection(ctx, event=event)
        self.assertEqual("private_companion.relationship_score", ctx.relationship_authority_source)
        self.assertEqual("interaction_dynamics.v1", ctx.companion_interaction_dynamics_version)
        self.assertEqual("recovering", ctx.companion_interaction_recovery_band)

    def test_invalid_dynamics_fail_closed(self) -> None:
        hostile = projection()
        hostile["current_interaction"]["expires_at"] = "NaN"
        self.assertEqual("invalid", sanitize_companion_relationship_projection(hostile)["status"])


if __name__ == "__main__":
    unittest.main()
