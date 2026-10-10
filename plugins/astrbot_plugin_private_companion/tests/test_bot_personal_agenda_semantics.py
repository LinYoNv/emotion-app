from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "agenda_dto_contract_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package

from agenda_dto_contract_tests.bot_personal_dto import build_bot_personal_dto
from schedule_authority import ScheduleAuthorityAdapter


NOW = datetime.fromisoformat("2026-07-30T21:34:00+08:00")


def _build(memory_type: str, payload: dict, key: str):
    return build_bot_personal_dto(
        memory_type=memory_type,
        payload=payload,
        idempotency_key=key,
        occurred_at="2026-07-30T21:34:00+08:00",
        now=NOW,
    )


def test_schedule_plan_keeps_commitment_only_for_adapter_marked_reference() -> None:
    adapter = ScheduleAuthorityAdapter(clock=lambda: NOW)
    ref = adapter.issue_or_update(
        {
            "authority_kind": "timetable",
            "event_id": "class-1",
            "revision": "1",
            "timezone": "Asia/Shanghai",
            "updated_at": "2026-07-30T21:34:00+08:00",
            "effective_from": "2026-07-31T09:00:00+08:00",
            "effective_to": "2026-07-31T10:00:00+08:00",
        },
        "bot_self",
    )
    assert hasattr(ref, "to_plan_fields")
    trusted = _build(
        "bot_schedule_plan",
        {
            "date": "2026-07-31",
            **ref.to_plan_fields(),
            "status": "completed",
            "subject_actor_id": "bot_self",
        },
        "daily_plan:trusted",
    )
    assert trusted.status == "planned"
    assert trusted.fact_eligibility == "schedule_commitment"
    assert trusted.commitment_level == "confirmed"

    untrusted = _build(
        "bot_schedule_plan",
        {
            "date": "2026-07-31",
            "source_refs": ["model-invented"],
            "authority_kind": "timetable",
            "commitment_level": "confirmed",
            "subject_actor_id": "bot_self",
        },
        "daily_plan:untrusted",
    )
    assert untrusted.status == "planned"
    assert untrusted.fact_eligibility == "none"
    assert untrusted.commitment_level == "tentative"


def test_projection_reconciliation_and_detail_do_not_become_history() -> None:
    snapshot = _build(
        "bot_window_snapshot",
        {"date": "2026-07-30", "window": "evening", "status": "completed"},
        "agenda_snapshot:2026-07-30:evening",
    )
    assert snapshot.status == "reconciled"
    assert snapshot.fact_eligibility == "none"

    reconciliation = _build(
        "bot_schedule_reconciliation",
        {
            "date": "2026-07-30",
            "window": "evening",
            "status": "completed",
            "fact_eligibility": "history_observed",
        },
        "reconciliation:2026-07-30:evening",
    )
    assert reconciliation.status == "reconciled"
    assert reconciliation.fact_eligibility == "none"

    detail = _build(
        "bot_detail_fragment",
        {
            "date": "2026-07-30",
            "window": "evening",
            "summary": "possible scene",
            "status": "active",
            "fact_eligibility": "history_observed",
            "content_granularity": "scene",
            "subject_actor_id": "bot_self",
        },
        "detail:2026-07-30:1260:1320",
    )
    assert detail.status == "planned"
    assert detail.fact_eligibility == "none"
    assert detail.materialization_state == "candidate"
    assert detail.expires_at


def test_calendar_event_without_trusted_schedule_ref_is_not_a_commitment() -> None:
    event = _build(
        "bot_calendar_event",
        {
            "date": "2026-07-31",
            "window": "morning",
            "authority_kind": "calendar",
            "commitment_level": "confirmed",
            "source_refs": ["calendar:invented"],
            "subject_actor_id": "bot_self",
        },
        "calendar:event-1",
    )
    assert event.status == "planned"
    assert event.fact_eligibility == "none"
    assert event.commitment_level == "tentative"
