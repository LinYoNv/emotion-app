from __future__ import annotations

from person_context_contract import build_identity_key, ensure_person_store
from unified_person_registry import UnifiedPersonRegistry


def identity(platform="qq", subject="u-1", companion="c-1", bot="b-1", adapter="a-1"):
    return {
        "companion_instance_id": companion,
        "bot_account_id": bot,
        "adapter_instance_id": adapter,
        "subject_namespace": platform,
        "platform_subject_id": subject,
    }


def test_five_dimension_identity_and_stable_id():
    a, b = {}, {}
    left, right = UnifiedPersonRegistry(a), UnifiedPersonRegistry(b)
    one = left.create_or_link(identity(), {"display_name": "小雪"}, operation_id="create-1")
    two = right.create_or_link(identity(), {"display_name": "小雪"}, operation_id="create-2")
    assert one["person_id"] == two["person_id"]
    assert left.resolve(identity())["state"] == "resolved"
    assert left.resolve(identity("qq", "u-1", "c-1", "b-2", "a-1"))["state"] == "pending"


def test_duplicate_create_is_idempotent_and_projection_valid():
    store = {}
    registry = UnifiedPersonRegistry(store)
    first = registry.create_or_link(identity(), operation_id="create-1")
    second = registry.create_or_link(identity(), {"display_name": "other"}, operation_id="create-2")
    assert first["person_id"] == second["person_id"]
    assert second["changed"] is False
    projection = registry.read_projection(first["person_id"])
    assert projection and projection["person_id"] == first["person_id"]


def test_malformed_affinity_score_defaults_to_neutral_without_aborting_create():
    registry = UnifiedPersonRegistry({})
    result = registry.create_or_link(
        identity(),
        {"display_name": "profile", "affinity_score": "not-a-number"},
        operation_id="create-invalid-affinity",
    )

    assert result["ok"] is True
    assert result["projection"]["affinity_score"] == 0
    assert registry.read_projection(result["person_id"])["affinity_score"] == 0


def test_profile_creation_normalizes_invalid_owner_mode_without_persisting_an_invalid_projection():
    store = {}
    registry = UnifiedPersonRegistry(store)

    result = registry.create_or_link(
        identity("qq", "invalid-owner-mode"),
        profile={"owner_mode": "administrator", "display_name": {"raw": "hidden"}},
        operation_id="create-invalid-owner-mode",
    )

    assert result["ok"] is True
    assert result["projection"]["owner_mode"] == "not_owner"
    assert result["projection"]["display_name"] == "unknown_person"
    stored = store["unified_person"]["profiles"][result["person_id"]]
    assert stored["owner_mode"] == "not_owner"


def test_create_and_link_fail_without_partial_writes_for_corrupt_person_records():
    store = {}
    registry = UnifiedPersonRegistry(store)
    primary = identity("qq", "corrupt-person")
    person_id = registry.create_or_link(primary, operation_id="create-corrupt-person")["person_id"]
    root = store["unified_person"]
    original_links = set(root["identity_links"])
    root["profiles"][person_id]["identity_keys"] = "broken"

    result = registry.link_identity(
        person_id,
        identity("telegram", "corrupt-person"),
        operation_id="link-corrupt-person",
    )

    assert result["code"] == "person_record_invalid"
    assert set(root["identity_links"]) == original_links
    assert not any(
        item.get("event_id") == "link-corrupt-person"
        for item in root["audit_events"]
        if isinstance(item, dict)
    )

    root["identity_links"].pop(next(iter(original_links)))
    conflict = registry.create_or_link(primary, operation_id="recreate-corrupt-person")
    assert conflict["code"] == "person_record_conflict"
    assert root["profiles"][person_id]["identity_keys"] == "broken"


def test_link_projection_and_pending_without_explicit_operation():
    store = {}
    registry = UnifiedPersonRegistry(store)
    assert registry.create_or_link(identity(), operation_id="create")["state"] == "resolved"
    person_id = registry.resolve(identity())["person_id"]
    result = registry.link_identity(person_id, identity("telegram", "t-1"), operation_id="link-1")
    assert result["state"] == "resolved"
    assert registry.resolve(identity("telegram", "t-1"))["person_id"] == person_id
    assert registry.create_or_link(identity("qq", "new"))["state"] == "pending"


def test_unlink_recomputes_assurance_from_remaining_active_links():
    store = {}
    registry = UnifiedPersonRegistry(store)
    primary = identity()
    secondary = identity("telegram", "t-assurance")
    person_id = registry.create_or_link(primary, operation_id="create-assurance")["person_id"]
    assert registry.link_identity(person_id, secondary, operation_id="link-assurance")["ok"] is True
    assert registry.read_projection(person_id)["identity_assurance"] == "explicit_linked"

    result = registry.unlink_identity(
        person_id,
        secondary,
        operation_id="unlink-assurance",
        dry_run=False,
    )

    assert result["code"] == "identity_unlinked"
    assert registry.read_projection(person_id)["identity_assurance"] == "observed"


def test_unlink_operation_id_is_request_bound_and_legacy_records_still_replay():
    store = {}
    registry = UnifiedPersonRegistry(store)
    primary = identity()
    first_secondary = identity("telegram", "t-operation-1")
    second_secondary = identity("matrix", "m-operation-2")
    person_id = registry.create_or_link(primary, operation_id="create-operation")["person_id"]
    assert registry.link_identity(person_id, first_secondary, operation_id="link-operation-1")["ok"] is True
    assert registry.link_identity(person_id, second_secondary, operation_id="link-operation-2")["ok"] is True

    applied = registry.unlink_identity(
        person_id,
        first_secondary,
        operation_id="unlink-request-bound",
        dry_run=False,
    )
    replayed = registry.unlink_identity(
        person_id,
        first_secondary,
        operation_id="unlink-request-bound",
        dry_run=False,
    )
    conflict = registry.unlink_identity(
        person_id,
        second_secondary,
        operation_id="unlink-request-bound",
        dry_run=False,
    )

    assert replayed == applied
    assert conflict["code"] == "operation_id_conflict"
    assert registry.resolve(second_secondary)["state"] == "resolved"
    other_person_id = registry.create_or_link(
        identity("qq", "other-person"),
        operation_id="create-other-person",
    )["person_id"]
    assert registry.unlink_identity(
        other_person_id,
        identity("qq", "other-person"),
        operation_id="unlink-request-bound",
        dry_run=False,
    )["code"] == "operation_id_conflict"
    operation_key = "req036.unlink:unlink-request-bound"
    operation = store["unified_person"]["operations"][operation_key]
    assert set(operation) == {"request_fingerprint", "result"}

    # Records created by 6.0.5a stored the result directly.  Preserve replay
    # for the same target while still rejecting cross-target reuse.
    store["unified_person"]["operations"][operation_key] = operation["result"]
    assert registry.unlink_identity(
        person_id,
        first_secondary,
        operation_id="unlink-request-bound",
        dry_run=False,
    ) == applied
    assert registry.unlink_identity(
        person_id,
        second_secondary,
        operation_id="unlink-request-bound",
        dry_run=False,
    )["code"] == "operation_id_conflict"

    store["unified_person"]["operations"][operation_key] = "broken"
    assert registry.unlink_identity(
        person_id,
        second_secondary,
        operation_id="unlink-request-bound",
        dry_run=False,
    )["code"] == "operation_record_corrupt"
    assert registry.resolve(second_secondary)["state"] == "resolved"


def test_group_overlay_isolated_and_safe():
    store = {}
    registry = UnifiedPersonRegistry(store)
    person_id = registry.create_or_link(identity(), operation_id="create")["person_id"]
    first = registry.upsert_group_overlay(person_id, "qq:group-1", {"nickname": "甲", "raw_prompt": "secret", "chat_text": "private"}, operation_id="g1")
    second = registry.upsert_group_overlay(person_id, "qq:group-2", {"nickname": "乙"}, operation_id="g2")
    assert first["state"] == second["state"] == "resolved"
    assert registry.read_group_overlay(person_id, "qq:group-1")["overlay"] == {"nickname": "甲"}
    assert registry.read_group_overlay(person_id, "qq:group-2")["overlay"] == {"nickname": "乙"}
    assert registry.read_group_overlay(person_id, "qq:group-3") is None
    assert "chat_text" not in str(store)
    assert "raw_prompt" not in str(store)


def test_invalid_identity_and_corrupt_projection_are_explicit():
    store = {}
    registry = UnifiedPersonRegistry(store)
    assert registry.resolve({})["state"] == "invalid"
    ensure_person_store(store)
    store["unified_person"]["identity_links"]["bad"] = {"person_id": "person_bad"}
    assert registry.status()["state"] in {"pending", "invalid"}
