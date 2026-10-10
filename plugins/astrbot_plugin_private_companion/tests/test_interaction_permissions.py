# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.interaction_utils import InteractionUtilsMixin


class _Event:
    def __init__(self, sender_id: str, *, private: bool = True, scoped_id: str = "") -> None:
        self.sender_id = sender_id
        self.private = private
        self.scoped_id = scoped_id

    def get_sender_id(self) -> str:
        return self.sender_id

    def is_private_chat(self) -> bool:
        return self.private


class _ReplyEvent:
    def __init__(self) -> None:
        self.sent: list[object] = []

    @staticmethod
    def plain_result(text: str) -> tuple[str, str]:
        return ("plain", text)

    async def send(self, result: object) -> None:
        self.sent.append(result)


class _PermissionHarness(InteractionUtilsMixin):
    def __init__(self) -> None:
        self.target_user_ids = ["configured-target"]
        self.data = {
            "users": {
                "role-owner": {"relationship_role": "owner"},
                "role-friend": {"relationship_role": "friend"},
                "canonical-owner": {"relationship_role": "owner"},
            }
        }

    @staticmethod
    def _normalize_private_identity_id(value) -> str:
        return str(value or "").strip()

    @staticmethod
    def _normalize_private_user_role(value) -> str:
        role = str(value or "").strip().lower()
        return role if role in {"owner", "friend"} else ""

    def _configured_target_ids(self) -> list[str]:
        return list(self.target_user_ids)

    @staticmethod
    def _event_permission_identity_id(event: _Event) -> str:
        return event.scoped_id or event.sender_id

    @staticmethod
    def _configured_admin_ids() -> set[str]:
        return {"astrbot-admin"}


class InteractionPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = _PermissionHarness()

    def test_relationship_owner_is_plugin_manager(self) -> None:
        self.assertEqual({"role-owner", "canonical-owner"}, self.plugin._relationship_owner_user_ids())
        self.assertTrue(self.plugin._is_private_companion_owner_user_id("role-owner"))
        self.assertTrue(self.plugin._is_plugin_manager_user_id("role-owner"))

    def test_existing_manager_sources_remain_allowed(self) -> None:
        self.assertTrue(self.plugin._is_plugin_manager_user_id("configured-target"))
        self.assertTrue(self.plugin._is_plugin_manager_user_id("astrbot-admin"))

    def test_friend_role_is_not_plugin_manager(self) -> None:
        self.assertFalse(self.plugin._is_private_companion_owner_user_id("role-friend"))
        self.assertFalse(self.plugin._is_plugin_manager_user_id("role-friend"))

    def test_alias_identity_does_not_inherit_owner_permission(self) -> None:
        self.assertFalse(self.plugin._is_private_companion_owner_user_id("owner-alias"))
        self.assertFalse(self.plugin._is_plugin_manager_user_id("owner-alias"))

    def test_sensitive_location_only_allows_owner_in_private_chat(self) -> None:
        self.assertTrue(self.plugin._can_manage_sensitive_location(_Event("configured-target")))
        self.assertTrue(self.plugin._can_manage_sensitive_location(_Event("role-owner")))
        self.assertFalse(self.plugin._can_manage_sensitive_location(_Event("configured-target", private=False)))
        self.assertFalse(self.plugin._can_manage_sensitive_location(_Event("astrbot-admin")))
        self.assertFalse(self.plugin._can_manage_sensitive_location(_Event("role-friend")))
        self.assertFalse(self.plugin._can_manage_sensitive_location(_Event("owner-alias")))

    def test_sensitive_location_denial_does_not_disclose_configuration(self) -> None:
        denial = self.plugin._sensitive_location_denied_text()
        self.assertNotIn("当前", denial)
        self.assertNotIn("绑定城市", denial)
        self.assertNotIn("LocationID", denial)

    def test_event_scoped_identity_does_not_inherit_owner_from_same_raw_id(self) -> None:
        self.plugin.data["users"]["onebot:123:isolated"] = {
            "relationship_role": "owner",
            "identity_subject_id": "123",
            "identity_platform_kind": "onebot",
        }
        self.plugin.data["users"]["123"] = {
            "relationship_role": "owner",
            "identity_subject_id": "123",
            "identity_platform_kind": "onebot",
        }
        self.assertTrue(self.plugin._can_manage_private_companion(_Event("123", scoped_id="123")))
        self.assertFalse(
            self.plugin._can_manage_private_companion(
                _Event("123", scoped_id="qq_official:123:isolated")
            )
        )


class InteractionReplyStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_reports_cancelled_and_sent_states(self) -> None:
        plugin = _PermissionHarness()
        event = _ReplyEvent()

        async def cancelled(_event: object) -> str:
            return "missing-message"

        plugin._should_cancel_reply_for_missing_or_recalled_trigger = cancelled
        plugin._group_current_reply_quote_message_id = lambda *_args, **_kwargs: ""
        self.assertFalse(await plugin._reply(event, "拒绝文本"))
        self.assertEqual([], event.sent)

        async def allowed(_event: object) -> str:
            return ""

        plugin._should_cancel_reply_for_missing_or_recalled_trigger = allowed
        self.assertTrue(await plugin._reply(event, "拒绝文本"))
        self.assertEqual([("plain", "拒绝文本")], event.sent)


if __name__ == "__main__":
    unittest.main()
