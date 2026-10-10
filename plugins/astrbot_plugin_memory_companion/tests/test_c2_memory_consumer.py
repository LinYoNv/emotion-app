from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context_consumer import consume_context_projection
from core.person_context_contract import (
    P3_SLOT_NAMES,
    build_context_projection,
    build_identity_key,
    build_person_projection,
    empty_person_store,
    make_context_slot,
    person_id_for_identity,
)
from core.person_projection import consume_person_projection


IDENTITY = {
    "companion_instance_id": "companion-test",
    "bot_account_id": "bot-1",
    "adapter_instance_id": "adapter-1",
    "subject_namespace": "qq",
    "platform_subject_id": "user-42",
}


def _projection():
    store = {"unified_person": empty_person_store()}
    person_id = person_id_for_identity(IDENTITY)
    identity_key = build_identity_key(IDENTITY)
    store["unified_person"]["profiles"][person_id] = {
        "person_id": person_id,
        "resolved_identity_key": identity_key,
        "identity_assurance": "verified",
        "profile_status": "active",
        "display_name": "Alice",
        "aliases": ["A"],
        "owner_mode": "not_owner",
        "affinity_score": 12,
        "projection_revision": 3,
        "relation_policy_id": "default_friend",
        "group_overlay_ref": "",
        "updated_at": "2026-07-30T00:00:00+00:00",
    }
    store["unified_person"]["identity_links"][identity_key] = {"person_id": person_id}
    return build_person_projection(store, person_id), store, person_id, identity_key


def _context(*, revision=4, person_id="person_abc", scope="private"):
    slots = {
        name: make_context_slot(
            name,
            {"person_id": person_id, "scope": scope, "label": name, "raw_prompt": "do not leak"},
            revision=revision,
        )
        for name in P3_SLOT_NAMES
    }
    context = build_context_projection(slots, revision=revision)
    context["person_id"] = person_id
    context["scope"] = scope
    return context


class C2MemoryConsumerTests(unittest.TestCase):
    def test_projection_is_resolved_and_only_returns_safe_reference(self) -> None:
        projection, store, person_id, identity_key = _projection()
        result = consume_person_projection(
            projection,
            expected_identity_key=identity_key,
            expected_person_id=person_id,
            identity_store=store,
            identity=IDENTITY,
        )
        self.assertEqual("resolved", result["state"])
        self.assertEqual(person_id, result["projection_ref"]["person_id"])
        self.assertNotIn("display_name", result["projection_ref"])
        self.assertTrue(result["read_only"])

    def test_projection_rejects_version_fingerprint_and_expected_identity_mismatch(self) -> None:
        projection, _, person_id, identity_key = _projection()
        bad = deepcopy(projection)
        bad["contract_fingerprint"] = "wrong"
        self.assertEqual("invalid", consume_person_projection(bad)["state"])
        self.assertIn("contract_fingerprint_mismatch", consume_person_projection(bad)["errors"])
        mismatch = consume_person_projection(projection, "wrong", person_id)
        self.assertEqual("invalid", mismatch["state"])
        self.assertIn("identity_key_mismatch", mismatch["errors"])
        self.assertEqual(
            "invalid",
            consume_person_projection(projection, identity_key, "person_000000000000000000000000")["state"],
        )

    def test_projection_unavailable_is_explicitly_degraded(self) -> None:
        projection, _, _, _ = _projection()
        result = consume_person_projection(projection, companion_available=False)
        self.assertEqual("degraded", result["state"])
        self.assertTrue(result["degraded"])

    def test_context_consumes_four_owned_slots_and_strips_raw_text(self) -> None:
        result = consume_context_projection(_context(), "person_abc", "private")
        self.assertEqual("ready", result["state"])
        self.assertEqual(set(P3_SLOT_NAMES), set(result["context_ref"]["slots"]))
        for slot in result["context_ref"]["slots"].values():
            self.assertNotIn("raw_prompt", slot["payload"])
            self.assertTrue(slot["owner"])

    def test_context_rejects_wrong_owner_and_domain_projection(self) -> None:
        context = _context()
        context["slots"]["person"]["owner"] = "companion"
        result = consume_context_projection(context, "person_abc", "private")
        self.assertEqual("invalid", result["state"])
        self.assertIn("person", result["rejected_slots"])

        context = _context(scope="group:one")
        self.assertEqual("invalid", consume_context_projection(context, "person_abc", "group:two")["state"])

    def test_context_rejects_missing_top_level_identity_binding(self) -> None:
        missing_person = _context()
        del missing_person["person_id"]
        person_result = consume_context_projection(missing_person, "person_abc", "private")
        self.assertEqual("invalid", person_result["state"])
        self.assertIn("person_id_mismatch", person_result["errors"])

        missing_scope = _context()
        del missing_scope["scope"]
        scope_result = consume_context_projection(missing_scope, "person_abc", "private")
        self.assertEqual("invalid", scope_result["state"])
        self.assertIn("scope_mismatch", scope_result["errors"])

    def test_context_rejects_slots_without_exact_expected_identity_binding(self) -> None:
        cases = (
            ("runtime", "person_id", None, "person_id_mismatch"),
            ("runtime", "person_id", "person_other", "person_id_mismatch"),
            ("scene", "scope", None, "scope_mismatch"),
            ("scene", "scope", "group:other", "scope_mismatch"),
        )
        for slot_name, field, value, error in cases:
            with self.subTest(slot=slot_name, field=field, value=value):
                context = _context()
                payload = context["slots"][slot_name]["payload"]
                if value is None:
                    del payload[field]
                else:
                    payload[field] = value

                result = consume_context_projection(context, "person_abc", "private")

                self.assertEqual("ready", result["state"])
                self.assertEqual([error], result["rejected_slots"][slot_name])
                self.assertEqual({}, result["context_ref"]["slots"][slot_name]["payload"])

    def test_context_deduplicates_by_revision_and_handles_legacy_or_bridge_degraded(self) -> None:
        self.assertEqual("legacy_local", consume_context_projection(None)["state"])
        self.assertEqual("degraded", consume_context_projection({}, companion_available=False)["state"])
        newer_result = consume_context_projection(_context(revision=7), "person_abc", "private")
        older_result = consume_context_projection(_context(revision=2), "person_abc", "private")
        self.assertEqual(7, newer_result["context_ref"]["revision"])
        self.assertEqual(2, older_result["context_ref"]["revision"])

    def test_context_preserves_explicit_degraded_pending_and_invalid_states(self) -> None:
        degraded = _context()
        degraded["state"] = "degraded"
        degraded["slots"]["runtime"]["state"] = "degraded"
        self.assertEqual("degraded", consume_context_projection(degraded, "person_abc", "private")["state"])

        pending = _context()
        pending["state"] = "pending"
        pending["slots"]["person"]["state"] = "pending"
        self.assertEqual("pending", consume_context_projection(pending, "person_abc", "private")["state"])

        invalid = _context()
        invalid["slots"]["person"]["state"] = "invalid"
        result = consume_context_projection(invalid, "person_abc", "private")
        self.assertEqual("invalid", result["state"])
        self.assertIn("person_state_invalid", result["errors"])
