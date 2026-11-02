"""The role-driven scope preview must describe the same windows the importer would take."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_host.identity_derivation import derive_role_id
from astrbot_host.legacy_api import LegacyMigrationWebAPI
from companion_v2.foundation.outcome import Outcome


class ScopePreviewTests(unittest.TestCase):
    def api(self, role_id):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = SimpleNamespace(
            role_id=role_id,
            config={"multi_host": {"personas": [{"persona_id": "astrbot", "role_id": role_id}]}},
            web_api=SimpleNamespace(target_inventory=lambda: [
                {"scope_kind": "group", "target_id": "group-109", "group_id": "109",
                 "platform_id": "qq", "account_id": "100",
                 "roles": [{"role_id": role_id, "legacy_group_data_api": "/api/group"}]},
                {"scope_kind": "group", "target_id": "group-915", "group_id": "915",
                 "platform_id": "qq", "account_id": "100", "roles": []},
                {"scope_kind": "private", "target_id": "private", "roles": []},
            ]))
        return api

    def session(self):
        scope = SimpleNamespace(installation_id="installation", platform="qq", bot_id="bot",
                                account_id="100", kind="private", native_scope_id="200")
        context = SimpleNamespace(scope=scope, role_binding=SimpleNamespace(
            role=SimpleNamespace(subject_id="role-a")))
        return SimpleNamespace(profile=SimpleNamespace(target=context),
                               application=SimpleNamespace(semantics=object()))

    def test_scope_previews_personas_groups_and_counts_without_writing(self):
        role_id = derive_role_id("100", "[%None]")
        api = self.api(role_id)
        documents = [
            {"persona_sources": [{"persona_id": "astrbot", "state": "ready"},
                                 {"persona_id": "old", "state": "ready"}],
             "group_networks": [], "private_contacts": {"importable": 2, "rejected": []}},
            {"group_networks": [{"source_group_id": "109", "members": 3, "edges": 2}],
             "private_contacts": {"importable": 0}},
            {"group_networks": [], "private_contacts": {}},
        ]
        migration = SimpleNamespace(discover=lambda: Outcome.success(True), v1_configuration=None)
        with patch("companion_v2.legacy.data_migration.build_data_plan",
                   side_effect=[SimpleNamespace(document=lambda item=item: item) for item in documents]):
            result = api._scope(migration, None, self.session())
        self.assertTrue(result.is_success)
        payload = result.unwrap()
        self.assertEqual(payload["counts"]["personas"], 3)
        self.assertEqual(payload["counts"]["personas_auto"], 2)
        self.assertEqual(payload["counts"]["groups"], 1)
        self.assertEqual(payload["counts"]["groups_auto"], 1)
        self.assertEqual(payload["groups"][0]["persona_id"], "astrbot")
        self.assertEqual(payload["groups"][0]["target_id"], "group-109")
        self.assertEqual(payload["groups"][0]["counts"]["members"], 3)
        self.assertEqual(payload["private"], {"contacts": 2, "rejected": 0})
        personas = {item["source_id"]: item for item in payload["personas"]}
        self.assertTrue(personas[""]["auto"])
        self.assertTrue(personas["astrbot"]["auto"])
        self.assertFalse(personas["old"]["auto"])
        self.assertEqual([item["source_id"] for item in payload["review"]], ["old"])
        self.assertTrue(payload["token"])

    def test_scope_rejects_unreadable_sources_instead_of_inventing_windows(self):
        api = self.api("role-a")
        migration = SimpleNamespace(discover=lambda: Outcome.failed("legacy_source_missing", "gone"),
                                    v1_configuration=None)
        result = api._scope(migration, None, self.session())
        self.assertFalse(result.is_success)
        self.assertEqual(result.failure.code, "legacy_source_missing")


class BatchApplyTests(unittest.IsolatedAsyncioTestCase):
    def api(self):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = SimpleNamespace(role_id="role-a")
        return api

    async def test_stale_scope_writes_nothing(self):
        api = self.api()
        scope = {"token": "new", "personas": [], "groups": []}
        apply = AsyncMock()
        with patch.object(LegacyMigrationWebAPI, "_scope", return_value=Outcome.success(scope)), \
             patch.object(LegacyMigrationWebAPI, "_data_apply", apply):
            result = await api._batch_apply(None, None, None,
                                            {"token": "old", "personas": ["p"], "groups": []})
        self.assertFalse(result.is_success)
        self.assertEqual(result.failure.code, "legacy_scope_stale")
        apply.assert_not_called()

    async def test_review_items_are_reported_and_skipped(self):
        api = self.api()
        scope = {"token": "t",
                 "personas": [{"source_id": "auto", "auto": True},
                              {"source_id": "rev", "auto": False, "reason": "persona_not_assigned_to_role"}],
                 "groups": []}
        apply = AsyncMock(return_value=Outcome.success({"contacts": 1}))
        with patch.object(LegacyMigrationWebAPI, "_scope", return_value=Outcome.success(scope)), \
             patch.object(LegacyMigrationWebAPI, "_data_plan", return_value=Outcome.success({"plan_token": "pt"})), \
             patch.object(LegacyMigrationWebAPI, "_data_apply", apply):
            result = await api._batch_apply(None, None, None,
                                            {"token": "t", "personas": ["auto", "rev"], "groups": []})
        payload = result.unwrap()
        self.assertEqual([(item["persona_id"], item["state"]) for item in payload["items"]],
                         [("auto", "imported"), ("rev", "review")])
        self.assertEqual(payload["counts"], {"imported": 1, "review": 1, "failed": 0})
        self.assertEqual(payload["items"][1]["reason"], "persona_not_assigned_to_role")
        apply.assert_awaited_once()
        self.assertEqual(apply.await_args.args[3], "pt")

    async def test_auto_mapped_group_runs_the_shared_importer(self):
        api = self.api()
        scope = {"token": "t", "personas": [{"source_id": "auto", "auto": True}],
                 "groups": [{"persona_id": "auto", "source_id": "109", "target_id": "group-109",
                             "auto": True}]}
        calls = []

        class FakeGroupAPI:
            def __init__(self, controller, target_id, role_id):
                calls.append((target_id, role_id))

            def _target(self):
                return SimpleNamespace(target_id="group-109"), object()

            def _plan(self, root, persona_id, group_id, target, session):
                return {"plan_token": "gpt", "group_expression": {"available": True}}

            def apply_import(self, arguments, root, target, session):
                calls.append(arguments["source_group_id"], )
                return {"status": "ok", "data": {"idempotent": True}}

        with patch.object(LegacyMigrationWebAPI, "_scope", return_value=Outcome.success(scope)), \
             patch("astrbot_host.legacy_group_data_api.LegacyGroupDataWebAPI", FakeGroupAPI):
            result = await api._batch_apply(None, None, None,
                                            {"token": "t", "personas": [], "groups": [
                                                {"persona_id": "auto", "group_id": "109"}]})
        payload = result.unwrap()
        self.assertEqual(payload["items"][0]["state"], "imported")
        self.assertEqual(calls[0], ("group-109", "role-a"))
        self.assertEqual(calls[1], "109")


if __name__ == "__main__":
    unittest.main()
