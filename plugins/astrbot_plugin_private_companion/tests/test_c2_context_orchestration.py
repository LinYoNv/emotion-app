from __future__ import annotations

import importlib


def load(name: str):
    return importlib.import_module(name)


context = load("context_orchestration")
contract = load("person_context_contract")
shadow = load("p4_shadow")


def test_four_slots_have_contract_owners_and_revision():
    result = context.build_context(
        {"persona": {"tone": "warm"}, "runtime": {"channel": "private"},
         "person": {"person_id": "person_abc"}, "scene": {"scope": "private"}},
        revisions={"persona": 2, "runtime": 3, "person": 4, "scene": 5},
    )
    assert set(result["slots"]) == set(contract.P3_SLOT_NAMES)
    assert context.validate_context(result) == []
    for name in contract.P3_SLOT_NAMES:
        assert result["slots"][name]["owner"] == contract.P3_SLOT_OWNERS[name]
    assert result["slots"]["scene"]["revision"] == 5


def test_revision_replay_is_idempotent_and_older_update_does_not_replace():
    first = context.build_context({"persona": {"tone": "warm"}}, revisions={"persona": 2})
    replay = context.build_context({"persona": {"tone": "warm"}}, revisions={"persona": 2}, existing=first)
    older = context.build_context({"persona": {"tone": "cold"}}, revisions={"persona": 1}, existing=replay)
    assert replay == first
    assert older["slots"]["persona"]["payload"] == {"tone": "warm"}


def test_missing_and_bridge_degraded_are_explicit():
    assert context.build_context()["state"] == "legacy_local"
    degraded = context.build_context({"runtime": {}}, bridge_available=False)
    assert degraded["state"] == "degraded"
    assert all(slot["state"] == "degraded" for slot in degraded["slots"].values())


def test_person_slot_rejects_group_domain_facts():
    result = context.build_context({"person": {"display_name": "A", "group_id": "g1"}})
    slot = result["slots"]["person"]
    assert slot["state"] == "invalid"
    assert "person_group_domain_mixed" in slot["warnings"]
    assert slot["payload"] == {}


def test_contract_fingerprint_mismatch_is_rejected():
    result = context.build_context({"persona": {"tone": "warm"}})
    result["contract_fingerprint"] = "wrong"
    assert "context_fingerprint_mismatch" in context.validate_context(result)
    assert context.project_context(result)["state"] == "degraded"


def test_p4_shadow_has_required_metadata_and_no_raw_text():
    record = shadow.build_p4_shadow(
        source_kind="companion", target_kind="person", authority="runtime",
        reason_code="context_observed", safe_reference="person_abc",
        event_id="evt_1", timestamp="2026-07-30T00:00:00+00:00",
    )
    assert shadow.validate_p4_shadow(record) == []
    assert record["shadow_only"] is True
    assert "raw_prompt" not in record and "content" not in record


def test_p4_forbidden_input_is_explicit_invalid_and_not_copied():
    record = shadow.build_p4_shadow(
        source_kind="companion", target_kind="person", authority="runtime",
        reason_code="context_observed", event_id="evt_2",
        metadata={"raw_prompt": "secret full conversation"},
    )
    assert record["status"] == "invalid"
    assert "secret full conversation" not in repr(record)
    assert shadow.validate_p4_shadow(record) == []
