# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.constants import _DATA_STORE_KEYS
from astrbot_plugin_private_companion.core_store import CoreStoreMixin
from astrbot_plugin_private_companion.photo_generation_scope import (
    PHOTO_GENERATION_SCOPES,
    legacy_photo_generation_scope_limits,
    normalize_photo_generation_scope_limit,
)


class _Event:
    def __init__(self, sender_id: str, *, group_id: str = "") -> None:
        self.sender_id = sender_id
        self.group_id = group_id

    def get_sender_id(self) -> str:
        return self.sender_id


class _ScopeQuotaHarness(CoreStoreMixin):
    def __init__(self) -> None:
        self.day = "2026-08-10"
        self.data: dict = {}
        self.users = {
            "owner": {"user_id": "owner", "relationship_role": "owner"},
            "friend": {"user_id": "friend", "relationship_role": "friend"},
        }
        self.photo_generation_private_owner_max_daily = 2
        self.photo_generation_private_friend_max_daily = 3
        self.photo_generation_group_max_daily = 1
        self.photo_generation_proactive_max_daily = 1

    def _environment_today_key(self) -> str:
        return self.day

    @staticmethod
    def _extract_group_id_from_event(event: _Event) -> str:
        return event.group_id

    @staticmethod
    def _private_user_id_for_event(_event: _Event, user_id: str) -> str:
        return user_id

    @staticmethod
    def _canonical_private_user_id(user_id: str) -> str:
        return user_id

    def _get_user(self, user_id: str):
        return self.users.get(user_id)

    @staticmethod
    def _private_user_role(user, user_id: str = "") -> str:
        if isinstance(user, dict):
            return str(user.get("relationship_role") or "friend")
        return "owner" if user_id == "owner" else "friend"


class PhotoScopeConfigTests(unittest.TestCase):
    def test_limit_normalization_uses_three_state_range(self) -> None:
        cases = {
            -5: -1,
            -1: -1,
            0: 0,
            8: 8,
            200: 100,
            None: -1,
            "bad": -1,
            float("inf"): -1,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, normalize_photo_generation_scope_limit(raw))

    def test_legacy_scope_subset_maps_selected_to_unlimited(self) -> None:
        limits = legacy_photo_generation_scope_limits(["private_owner", "group"])

        self.assertEqual(
            {
                "private_owner": -1,
                "private_friend": 0,
                "group": -1,
                "proactive": 0,
            },
            limits,
        )

    def test_missing_legacy_scope_uses_new_unlimited_defaults(self) -> None:
        self.assertEqual(
            {scope: -1 for scope in PHOTO_GENERATION_SCOPES},
            legacy_photo_generation_scope_limits(None),
        )

    def test_explicit_empty_or_invalid_legacy_scope_remains_disabled(self) -> None:
        for raw in ([], "", ["unknown"], "unknown"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    {scope: 0 for scope in PHOTO_GENERATION_SCOPES},
                    legacy_photo_generation_scope_limits(raw),
                )

    def test_mapping_values_are_normalized_without_checkbox_conversion(self) -> None:
        limits = legacy_photo_generation_scope_limits(
            {
                "private_owner": 5,
                "private_friend": 0,
                "photo_generation_group_max_daily": -1,
                "proactive": 500,
            }
        )

        self.assertEqual(5, limits["private_owner"])
        self.assertEqual(0, limits["private_friend"])
        self.assertEqual(-1, limits["group"])
        self.assertEqual(100, limits["proactive"])


class PhotoScopeQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _ScopeQuotaHarness()

    def test_scope_detection_distinguishes_owner_friend_group_and_proactive(self) -> None:
        owner = self.harness.users["owner"]
        friend = self.harness.users["friend"]

        self.assertEqual(
            "private_owner",
            self.harness._photo_generation_scope(_Event("owner"), user=owner),
        )
        self.assertEqual(
            "private_friend",
            self.harness._photo_generation_scope(_Event("friend"), user=friend),
        )
        self.assertEqual("group", self.harness._photo_generation_scope(_Event("owner", group_id="g1")))
        self.assertEqual("proactive", self.harness._photo_generation_scope(user=owner, proactive=True))

    def test_unlimited_disabled_and_positive_limits_are_distinct(self) -> None:
        self.harness.photo_generation_private_owner_max_daily = -1
        self.assertIsNone(
            self.harness._photo_generation_scope_quota_left(user=self.harness.users["owner"])
        )
        self.assertTrue(
            self.harness._photo_generation_scope_allowed(user=self.harness.users["owner"])
        )

        self.harness.photo_generation_private_owner_max_daily = 0
        self.assertEqual(
            0,
            self.harness._photo_generation_scope_quota_left(user=self.harness.users["owner"]),
        )
        self.assertFalse(
            self.harness._photo_generation_scope_allowed(user=self.harness.users["owner"])
        )
        self.assertIn(
            "管理员已关闭",
            self.harness._photo_generation_scope_quota_block_message(user=self.harness.users["owner"]),
        )

        self.harness.photo_generation_private_owner_max_daily = 2
        self.assertEqual(
            2,
            self.harness._photo_generation_scope_quota_left(user=self.harness.users["owner"]),
        )

    def test_allowed_compatibility_gate_rejects_exhausted_positive_quota(self) -> None:
        owner = self.harness.users["owner"]
        owner_event = _Event("owner")

        self.harness._note_photo_generation_scope_attempt(owner_event, user=owner)
        self.assertTrue(self.harness._photo_generation_scope_allowed(owner_event, user=owner))

        self.harness._note_photo_generation_scope_attempt(owner_event, user=owner)
        self.assertEqual(0, self.harness._photo_generation_scope_quota_left(owner_event, user=owner))
        self.assertFalse(self.harness._photo_generation_scope_allowed(owner_event, user=owner))

    def test_usage_is_isolated_by_requester_and_scope(self) -> None:
        owner = self.harness.users["owner"]
        friend = self.harness.users["friend"]
        owner_event = _Event("owner")
        owner_group_event = _Event("owner", group_id="g1")

        self.harness._note_photo_generation_scope_attempt(owner_event, user=owner)
        self.assertEqual(1, self.harness._photo_generation_scope_quota_left(owner_event, user=owner))
        self.assertEqual(3, self.harness._photo_generation_scope_quota_left(_Event("friend"), user=friend))

        self.harness._note_photo_generation_scope_attempt(owner_group_event, user=owner)
        self.assertEqual(0, self.harness._photo_generation_scope_quota_left(owner_group_event, user=owner))
        self.assertEqual(
            1,
            self.harness._photo_generation_scope_quota_left(_Event("friend", group_id="g1"), user=friend),
        )
        self.assertEqual(1, self.harness._photo_generation_scope_quota_left(owner_event, user=owner))

    def test_proactive_usage_is_counted_against_target_user(self) -> None:
        owner = self.harness.users["owner"]

        self.harness._note_photo_generation_scope_attempt(proactive=True, user=owner)

        self.assertEqual(
            0,
            self.harness._photo_generation_scope_quota_left(proactive=True, user=owner),
        )

    def test_scope_counter_resets_on_plugin_timezone_day_change(self) -> None:
        owner = self.harness.users["owner"]
        self.harness._note_photo_generation_scope_attempt(user=owner)
        self.assertEqual(1, self.harness._photo_generation_scope_quota_left(user=owner))

        self.harness.day = "2026-08-11"
        self.assertEqual(2, self.harness._photo_generation_scope_quota_left(user=owner))
        self.harness._note_photo_generation_scope_attempt(user=owner)

        usage = self.harness.data["photo_generation_scope_attempts"]
        self.assertEqual("2026-08-11", usage["day"])
        self.assertEqual(1, usage["counts"]["private_owner"]["owner"])

    def test_daily_outfit_without_target_only_obeys_zero_disable(self) -> None:
        self.assertEqual(
            1,
            self.harness._photo_generation_scope_quota_left(proactive=True),
        )
        self.harness.photo_generation_proactive_max_daily = 0
        self.assertEqual(0, self.harness._photo_generation_scope_quota_left(proactive=True))

    def test_scope_attempts_are_in_persisted_store_keys(self) -> None:
        self.assertIn("photo_generation_scope_attempts", _DATA_STORE_KEYS)


if __name__ == "__main__":
    unittest.main()
