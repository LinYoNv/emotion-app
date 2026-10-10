from __future__ import annotations

from datetime import datetime
import hashlib
import json

import pytest

from core.bot_personal_dto import BotPersonalValidationError, build_bot_personal_archive


NOW = datetime.fromisoformat("2026-07-30T21:34:00+08:00")


def _ref(*, authority: str = "timetable") -> dict[str, object]:
    fields = {
        "namespace": "private_companion",
        "event_id": "class-1",
        "provider": "local",
        "revision": "1",
        "updated_at": "2026-07-30T21:34:00+08:00",
        "timezone": "Asia/Shanghai",
        "subject_actor_id": "bot_self",
        "effective_from": "2026-07-31T09:00:00+08:00",
        "effective_to": "2026-07-31T10:00:00+08:00",
        "state": "active",
        "authority_kind": authority,
    }
    raw = json.dumps(
        {
            "namespace": fields["namespace"],
            "event_id": fields["event_id"],
            "revision": fields["revision"],
            "provider": fields["provider"],
            "subject_actor_id": fields["subject_actor_id"],
            "updated_at": fields["updated_at"],
            "timezone": fields["timezone"],
            "effective_from": fields["effective_from"],
            "effective_to": fields["effective_to"],
            "expires_at": "",
            "authority_kind": fields["authority_kind"],
            "confirmation_event_id": "",
            "confirmation_actor_id": "",
            "proposition": "",
            "confirmed_at": "",
            "target_user_id": "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fields["ref_id"] = "trusted_schedule:" + hashlib.sha256(raw).hexdigest()[:24]
    return fields


def _build(memory_type: str, payload: dict[str, object], key: str, **extra: object):
    return build_bot_personal_archive(
        memory_type=memory_type,
        payload=payload,
        idempotency_key=key,
        occurred_at="2026-07-30T21:34:00+08:00",
        now=NOW,
        **extra,
    )


def test_plain_schedule_plan_cannot_be_promoted_by_model_fields() -> None:
    dto = _build(
        "bot_schedule_plan",
        {
            "date": "2026-07-31",
            "source_refs": ["model-invented"],
            "authority_kind": "timetable",
            "commitment_level": "confirmed",
            "status": "completed",
            "subject_actor_id": "bot_self",
        },
        "plan:untrusted",
    )
    assert dto.status == "planned"
    assert dto.source_kind == "planned"
    assert dto.evidence_kind == "none"
    assert dto.evidence_level == "L0"
    assert dto.fact_eligibility == "none"
    assert dto.commitment_level == "tentative"
    assert dto.payload["legacy_source_refs"] == ["model-invented"]


def test_adapter_shaped_schedule_ref_can_prove_only_a_commitment() -> None:
    ref = _ref()
    # This is the ref_id emitted by the companion's ScheduleAuthorityAdapter
    # for the fixture above; keep the literal to catch cross-plugin drift.
    assert ref["ref_id"] == "trusted_schedule:8fa5d3872c0627dbf1e49741"
    dto = _build(
        "bot_schedule_plan",
        {
            "date": "2026-07-31",
            "source_refs": [ref["ref_id"]],
            "schedule_ref": ref,
            "authority_kind": "timetable",
            "commitment_level": "confirmed",
            "subject_actor_id": "bot_self",
        },
        "plan:trusted",
    )
    assert dto.status == "planned"
    assert dto.commitment_level == "confirmed"
    assert dto.fact_eligibility == "schedule_commitment"
    assert dto.evidence_kind == "none"
    assert dto.canonical_evidence_level == "L0"


def test_projection_reconciliation_and_detail_are_not_history_by_default() -> None:
    snapshot = _build(
        "bot_window_snapshot",
        {"date": "2026-07-30", "window": "evening", "status": "completed"},
        "snapshot:evening",
    )
    assert snapshot.status == "reconciled"
    assert snapshot.fact_eligibility == "none"

    reconciliation = _build(
        "bot_schedule_reconciliation",
        {
            "date": "2026-07-30",
            "window": "evening",
            "status": "completed",
            "evidence_kind": "tool_action",
            "fact_eligibility": "history_observed",
            "source_refs": ["tool:event-1"],
        },
        "reconciliation:evening",
    )
    assert reconciliation.status == "reconciled"
    assert reconciliation.fact_eligibility == "none"
    assert reconciliation.evidence_kind == "none"

    detail = _build(
        "bot_detail_fragment",
        {
            "date": "2026-07-30",
            "window": "evening",
            "summary": "possible scene",
            "status": "active",
        },
        "detail:evening",
    )
    assert detail.status == "planned"
    assert detail.fact_eligibility == "none"
    assert detail.materialization_state == "candidate"
    assert detail.expires_at


def test_calendar_event_requires_a_valid_reference_and_l4_mapping_is_lossy() -> None:
    untrusted = _build(
        "bot_calendar_event",
        {
            "date": "2026-07-31",
            "window": "morning",
            "source_refs": ["calendar:invented"],
            "authority_kind": "calendar",
            "commitment_level": "confirmed",
        },
        "calendar:untrusted",
    )
    assert untrusted.fact_eligibility == "none"
    assert untrusted.commitment_level == "tentative"

    mapped = _build(
        "bot_observed_activity",
        {"summary": "tool observation", "source_refs": ["tool:1"], "fact_eligibility": "current_observed"},
        "observed:l4",
        canonical_evidence_level="L4",
    )
    assert mapped.canonical_evidence_level == "L4"
    assert mapped.archive_evidence_level == "L3"
    assert mapped.evidence_level_mapping["lossy"] is True


def test_bot_personal_archive_rejects_foreign_subject_actor() -> None:
    with pytest.raises(BotPersonalValidationError) as caught:
        _build(
            "bot_schedule_plan",
            {"subject_actor_id": "user-1", "summary": "user assertion"},
            "plan:foreign",
        )
    assert caught.value.error_code == "invalid"
    assert caught.value.field == "subject_actor_id"
