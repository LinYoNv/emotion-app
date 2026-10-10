from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from relationship_ledger import (
    apply_natural_relationship_decay,
    apply_relationship_event,
    record_manual_relationship_change,
    relationship_ledger_summary,
)


def _ts(day: int) -> float:
    return datetime(2026, 8, day, tzinfo=timezone.utc).timestamp()


def test_positive_events_are_deduplicated_and_bounded() -> None:
    user = {"relationship_role": "friend", "relationship_score": 1199}
    first = apply_relationship_event(user, 4, reason_code="inbound", now=_ts(1))
    duplicate = apply_relationship_event(user, 4, reason_code="inbound", now=_ts(1) + 60)
    assert first["changed"] is True
    assert user["relationship_score"] == 1200
    assert duplicate["code"] == "duplicate_event"


def test_owner_exclusive_is_frozen() -> None:
    user = {"relationship_role": "owner", "relationship_mode": "owner_exclusive", "relationship_score": 600}
    result = apply_relationship_event(user, 4, reason_code="warmth", now=_ts(1))
    assert result["code"] == "owner_exclusive_frozen"
    assert user["relationship_score"] == 600


def test_silence_decays_positive_score_without_creating_negative_score() -> None:
    user = {
        "relationship_role": "friend",
        "relationship_score": 30,
        "relationship_last_effective_at": _ts(1),
    }
    result = apply_natural_relationship_decay(user, now=_ts(10))
    assert result["changed"] is True
    assert 0 <= user["relationship_score"] < 30
    again = apply_natural_relationship_decay(user, now=_ts(10) + 60)
    assert again["code"] == "already_settled"


def test_negative_event_can_cross_zero_but_is_capped() -> None:
    user = {"relationship_role": "friend", "relationship_score": 3}
    result = apply_relationship_event(user, -100, reason_code="boundary_violation", now=_ts(1))
    assert result["delta"] == -12
    assert user["relationship_score"] == -9


def test_manual_adjustment_is_audited_without_reapplying_the_score() -> None:
    user = {"relationship_role": "friend", "relationship_score": 500}
    user["relationship_score"] = 650
    result = record_manual_relationship_change(user, 500, 650, now=_ts(1))
    summary = relationship_ledger_summary(user)
    assert result["changed"] is True
    assert user["relationship_score"] == 650
    assert summary["trend"] == "rising"
    assert summary["recent_delta"] == 150
    assert summary["items"][-1]["source"] == "administrator"


def test_zero_positive_daily_cap_disables_automatic_growth() -> None:
    user = {"relationship_role": "friend", "relationship_score": 100}
    result = apply_relationship_event(
        user,
        3,
        reason_code="friendly_exchange",
        now=_ts(1),
        event_id="zero-cap",
        positive_daily_cap=0,
    )
    assert result["changed"] is False
    assert result["code"] == "positive_daily_cap"
    assert user["relationship_score"] == 100


def test_high_relationship_stages_have_diminishing_positive_gain() -> None:
    low = {"relationship_role": "friend", "relationship_score": 100}
    close = {"relationship_role": "friend", "relationship_score": 700}
    intimate = {"relationship_role": "friend", "relationship_score": 1000}
    low_result = apply_relationship_event(low, 4, reason_code="support", now=_ts(1), event_id="low")
    close_result = apply_relationship_event(close, 4, reason_code="support", now=_ts(1), event_id="close")
    intimate_result = apply_relationship_event(intimate, 4, reason_code="support", now=_ts(1), event_id="intimate")
    assert low_result["delta"] == 4
    assert close_result["delta"] == 3
    assert intimate_result["delta"] == 2
