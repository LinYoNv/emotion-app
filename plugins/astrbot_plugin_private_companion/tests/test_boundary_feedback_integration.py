# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from astrbot_plugin_private_companion.relationship_ledger import apply_relationship_event
from astrbot_plugin_private_companion.user_memory import UserMemoryMixin


class _BoundaryHarness(UserMemoryMixin):
    enable_relationship_boundary_feedback = True
    enable_relationship_violation_penalties = True
    enable_custom_relationship_stage_policy = True
    enable_relationship_boundary_vent = False
    enable_relationship_boundary_owner_report = False
    relationship_violation_recovery_minutes_per_point = 15
    relationship_boundary_tier_adaptive = False

    def __init__(self) -> None:
        self.data = {
            "users": {},
            "daily_story_plan": {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "today_events": [],
                "proactive_events": [],
                "long_term_events": [],
            },
            "boundary_feedback_reports": [],
            "boundary_feedback_vent_history": [],
        }
        self.saved = 0

    @staticmethod
    def _private_user_role(user: dict, _user_id: str = "") -> str:
        return str(user.get("relationship_role") or "friend")

    @staticmethod
    def _configured_target_ids() -> list[str]:
        return ["owner-1"]

    def _schedule_data_save(self, *_args, **_kwargs) -> None:
        self.saved += 1

    @staticmethod
    def _apply_relationship_event(user: dict, delta: int, **kwargs):
        return apply_relationship_event(
            user,
            delta,
            positive_daily_cap=120,
            event_window_seconds=1,
            positive_event_cap=60,
            negative_event_cap=60,
            **kwargs,
        )


def _user(score: int = 0, role: str = "friend") -> dict:
    return {
        "user_id": "friend-1",
        "nickname": "小林",
        "relationship_role": role,
        "relationship_mode": "normal",
        "relationship_score": score,
        "relationship_score_schema_version": 2,
    }


def test_standalone_comfort_is_not_an_intimate_action() -> None:
    host = _BoundaryHarness()
    signal = host._classify_local_boundary_feedback_signal("摸摸", target_hint=True)
    assert signal["type"] == "normal"


def test_apology_rhetorical_question_does_not_restore_relationship() -> None:
    host = _BoundaryHarness()
    intent = host._analyze_inbound_intent("对不起有用吗")
    assert intent["emotion_event"] != "apology"


def test_confession_is_recorded_without_penalty() -> None:
    host = _BoundaryHarness()
    user = _user(200)
    signal = host._classify_local_boundary_feedback_signal("我真的喜欢你呀", target_hint=True)
    intent = host._enrich_boundary_feedback_intent(
        user,
        {
            "emotion_event": "praise",
            "emotion_target": "bot",
            "boundary_feedback_type": signal["type"],
            "boundary_suitable_tier": signal["suitable_tier"],
            "boundary_feedback_reason": signal["reason"],
            "boundary_feedback_confidence": signal["confidence"],
        },
    )
    result = host._apply_relationship_violation_policy(user, intent, event_id="confession-1", now=1_700_000_000)
    assert result["reason"] == "confession_feedback"
    assert user["relationship_score"] == 200
    assert user["relationship_violation"]["confession_count"] == 1


def test_explicit_intimate_action_uses_relationship_tier_gap() -> None:
    host = _BoundaryHarness()
    familiar = _user(300)
    signal = host._classify_local_boundary_feedback_signal("我想抱住你", target_hint=True)
    intent = host._enrich_boundary_feedback_intent(
        familiar,
        {
            "emotion_event": "comfort",
            "emotion_target": "bot",
            "boundary_feedback_type": signal["type"],
            "boundary_suitable_tier": signal["suitable_tier"],
            "boundary_feedback_reason": signal["reason"],
            "boundary_feedback_confidence": signal["confidence"],
        },
    )
    assert intent["emotion_event"] == "boundary_violation"
    assert intent["violation_severity"] == 2

    intimate = _user(950)
    accepted = host._enrich_boundary_feedback_intent(intimate, dict(intent, emotion_event="comfort"))
    assert accepted["boundary_feedback_kind"] == "accepted_for_tier"
    assert accepted["emotion_event"] == "comfort"


def test_owner_is_exempt_from_boundary_projection() -> None:
    host = _BoundaryHarness()
    owner = _user(0, role="owner")
    intent = host._enrich_boundary_feedback_intent(
        owner,
        {
            "boundary_feedback_type": "action",
            "boundary_suitable_tier": "beyond",
            "boundary_feedback_confidence": 0.99,
            "emotion_event": "neutral",
        },
    )
    assert intent["boundary_feedback_exempt"] is True
    assert intent["emotion_event"] == "neutral"


def test_third_bottom_line_event_demotes_one_relationship_tier() -> None:
    host = _BoundaryHarness()
    user = _user(720)
    for index in range(3):
        result = host._apply_relationship_violation_policy(
            user,
            {
                "emotion_event": "boundary_violation",
                "emotion_target": "bot",
                "emotion_confidence": 0.95,
                "violation_severity": 3,
                "violation_kind": "bottom_line",
                "emotion_reason": "恶意贬低珍视对象",
                "text": "测试底线事件",
            },
            event_id=f"bottom-{index}",
            now=1_700_000_000 + index * 10,
        )
        assert result["changed"] is True
    assert user["relationship_violation"]["bottom_line_count"] == 3
    assert user["relationship_score"] <= 599
    assert any(item.get("reason_code") == "relationship_bottom_line_demote" for item in user["relationship_ledger"])


def test_apology_recovery_does_not_consume_or_depend_on_daily_positive_quota() -> None:
    host = _BoundaryHarness()
    user = _user(10)
    host._apply_relationship_violation_policy(
        user,
        {
            "emotion_event": "boundary_violation",
            "emotion_target": "bot",
            "emotion_confidence": 0.95,
            "violation_severity": 2,
            "violation_kind": "intimate_overreach",
            "emotion_reason": "超出当前关系边界",
        },
        event_id="quota-violation",
        now=1_700_000_000,
    )
    totals = user["relationship_daily_totals"]
    totals["positive"] = 120
    before = user["relationship_score"]
    result = host._apply_relationship_violation_policy(
        user,
        {"emotion_event": "apology", "emotion_target": "bot"},
        event_id="quota-apology",
        now=1_700_000_001,
    )
    assert result["recovered"] > 0
    assert user["relationship_score"] > before
    assert totals["positive"] == 120


def test_owner_report_ability_consumes_only_pending_owner_report() -> None:
    host = _BoundaryHarness()
    host.enable_relationship_boundary_owner_report = True
    host.data["users"]["owner-1"] = {
        "user_id": "owner-1",
        "relationship_role": "owner",
        "umo": "default:FriendMessage:owner-1",
    }
    offender = _user(100)
    state = host._relationship_violation_state(offender)
    state.update(
        {
            "last_event_id": "report-1",
            "last_kind": "bottom_line",
            "last_severity": 3,
            "bottom_line_count": 1,
            "stage": "reflect",
        }
    )
    report = host._queue_relationship_boundary_owner_report(
        offender,
        {"emotion_reason": "踩到重要底线", "text": "不合适的话"},
        state,
        now=1_700_000_000,
    )
    ctx = {
        "user": host.data["users"]["owner-1"],
        "config": {"only_bottom_line": True, "max_chars": 40},
    }
    assert report
    assert host._relationship_boundary_report_ability_available(ctx) is False  # historical fixture is expired
    report["created_at"] = datetime.now().timestamp()
    assert host._relationship_boundary_report_ability_available(ctx) is True
    payload = host._relationship_boundary_report_ability_executor(ctx)
    assert payload["success"] is True
    assert report["status"] == "delivered"
    assert host._relationship_boundary_report_ability_available(ctx) is False
