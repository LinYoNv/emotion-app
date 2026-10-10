from __future__ import annotations

from datetime import datetime, timedelta

from runtime_scene_resolver import RuntimeSceneResolver
from schedule_authority import ScheduleAuthorityAdapter, TrustedScheduleRef
from schedule_reconciler import reconcile


def test_resolver_is_bot_only_idempotent_and_ttl_bound() -> None:
    now = datetime.fromisoformat("2026-07-30T21:34:00+08:00")
    resolver = RuntimeSceneResolver(bot_id="bot-1", clock=lambda: now, default_ttl_seconds=120)
    first = resolver.resolve_now(
        [{"activity": "可能刷穿搭", "subject_actor_id": "bot-1"}],
        conversation_state={"active": True},
        now=now,
    )
    second = resolver.resolve_now([], conversation_state=False, now=now)
    assert first == second
    assert first["actor_type"] == "bot"
    assert first["subject_actor_id"] == "bot-1"
    assert first["source_refs"] == []
    assert first["fact_eligibility"] == "current_internal"
    assert resolver.get_current(now=now + timedelta(seconds=121)) is None


def test_user_turn_interrupts_a_previous_internal_state() -> None:
    now = datetime.fromisoformat("2026-07-30T21:34:00+08:00")
    resolver = RuntimeSceneResolver(bot_id="bot-1", clock=lambda: now, default_ttl_seconds=120)
    resting = resolver.resolve_now(
        [{"activity": "rest", "actor_type": "bot", "subject_actor_id": "bot-1"}],
        conversation_state=False,
        now=now,
    )
    interrupted = resolver.resolve_now([], conversation_state=True, now=now)
    assert resting["state"] != interrupted["state"]
    assert "interruption:user_turn" in interrupted["runtime_origin_refs"]
    assert interrupted["state_version"] == resting["state_version"] + 1


def test_resolver_ignores_unbound_or_user_candidates() -> None:
    now = datetime.fromisoformat("2026-07-30T21:34:00+08:00")
    resolver = RuntimeSceneResolver(bot_id="bot-1", clock=lambda: now)
    result = resolver.resolve_now(
        [
            {"activity": "user is studying", "actor_type": "interlocutor_user", "subject_actor_id": "user-1"},
            {"activity": "unbound study scene"},
        ],
        conversation_state=False,
        now=now,
    )
    assert result is None


def test_schedule_authority_requires_structured_absolute_source_and_is_idempotent() -> None:
    now = datetime.fromisoformat("2026-07-30T21:34:00+08:00")
    adapter = ScheduleAuthorityAdapter(clock=lambda: now)
    rejected = adapter.issue_or_update(
        {"authority_kind": "calendar", "event_id": "class-1", "revision": "1"},
        "bot-1",
    )
    assert not rejected
    payload = {
        "authority_kind": "calendar",
        "event_id": "class-1",
        "revision": "1",
        "timezone": "Asia/Shanghai",
        "updated_at": "2026-07-30T21:34:00+08:00",
        "effective_from": "2026-07-31T09:00:00+08:00",
        "effective_to": "2026-07-31T10:00:00+08:00",
    }
    ref = adapter.issue_or_update(payload, "bot-1")
    assert isinstance(ref, TrustedScheduleRef)
    assert adapter.issue_or_update(payload, "bot-1") == ref
    assert adapter.verify(ref, now=now) == "valid"
    fields = ref.to_plan_fields()
    assert fields["commitment_level"] == "confirmed"
    assert fields["source_refs_trusted"] is True


def test_schedule_authority_rejects_forged_or_unstructured_confirmation() -> None:
    now = datetime.fromisoformat("2026-07-30T21:34:00+08:00")
    adapter = ScheduleAuthorityAdapter(clock=lambda: now)
    base = {
        "authority_kind": "user_confirmation",
        "event_id": "confirmed-1",
        "revision": "1",
        "timezone": "Asia/Shanghai",
        "updated_at": "2026-07-30T21:34:00+08:00",
        "effective_from": "2026-07-31T09:00:00+08:00",
        "effective_to": "2026-07-31T10:00:00+08:00",
    }
    assert not adapter.issue_or_update(base, "bot-1")
    ref = adapter.issue_or_update(
        {
            **base,
            "updated_at": "2026-07-30T21:34:00+08:00",
            "confirmation_event_id": "msg-1",
            "confirmation_actor_id": "user-1",
            "proposition": "Bot 明早九点有课",
            "confirmed_at": "2026-07-30T21:34:00+08:00",
        },
        "bot-1",
    )
    assert isinstance(ref, TrustedScheduleRef)
    forged = TrustedScheduleRef(**{**ref.as_dict(), "ref_id": "trusted_schedule:forged"})
    assert adapter.verify(forged, now=now) == "invalid"


def test_rescheduled_reference_invalidates_the_immutable_old_object() -> None:
    now = datetime.fromisoformat("2026-07-30T21:34:00+08:00")
    adapter = ScheduleAuthorityAdapter(clock=lambda: now)
    first = adapter.issue_or_update(
        {
            "authority_kind": "calendar",
            "event_id": "appointment-1",
            "revision": "1",
            "timezone": "Asia/Shanghai",
            "updated_at": "2026-07-30T21:34:00+08:00",
            "effective_from": "2026-07-31T09:00:00+08:00",
            "effective_to": "2026-07-31T10:00:00+08:00",
        },
        "bot-1",
    )
    assert isinstance(first, TrustedScheduleRef)
    replacement = adapter.revoke_or_reschedule(
        "appointment-1",
        "1",
        "moved",
        now,
        reschedule={
            "revision": "2",
            "effective_from": "2026-07-31T11:00:00+08:00",
            "effective_to": "2026-07-31T12:00:00+08:00",
        },
    )
    assert isinstance(replacement, TrustedScheduleRef)
    assert adapter.verify(first, now=now) == "revoked"
    assert adapter.verify(replacement, now=now) == "valid"


def test_untrusted_plan_reference_does_not_trigger_execution_reconciliation() -> None:
    result = reconcile(
        [
            {
                "plan_id": "soft-refs",
                "title": "整理桌面",
                "source_refs": ["model-invented-ref"],
                "start_at": "2026-07-30T20:00:00+08:00",
                "end_at": "2026-07-30T21:00:00+08:00",
            }
        ],
        [
            {
                "activity_id": "activity-1",
                "title": "整理桌面",
                "source": "conversation",
                "source_refs": ["model-invented-ref"],
                "status": "completed",
                "start_at": "2026-07-30T20:00:00+08:00",
                "end_at": "2026-07-30T21:00:00+08:00",
            }
        ],
        now=datetime.fromisoformat("2026-07-30T21:34:00+08:00"),
    )
    assert result["matched"] == {}
    assert result["plans"][0]["status"] == "unknown"
