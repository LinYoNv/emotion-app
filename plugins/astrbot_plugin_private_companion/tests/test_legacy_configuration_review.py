"""The questionnaire's selections must match persisted settings, without model calls."""

from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from companion_v2.execution.models import TaskError
from companion_v2.foundation.outcome import Outcome
from companion_v2.legacy.migration import ConfigIntent, SourceFinding
from astrbot_host.legacy_configuration_review import configuration_rows, selected_intents, selection_id
from tests.proactive_support import ContactFixture
from tests.test_legacy_extension_registry_configuration import RegistryHostMigrationTests


class ConfigurationQuestionnaireTests(ContactFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await RegistryHostMigrationTests.asyncSetUp(self)
        original = RegistryHostMigrationTests.plan(self)
        required = [SourceFinding(name, "test", "configure", "absent", ()) for name in (
            "private.host_configuration", "image.configuration", "reality.configuration")]
        self.plan = replace(original, findings=tuple(required) + tuple(original.findings))
        self.migration = NS(plan=lambda: Outcome.success(self.plan))
        self.api._fingerprint = lambda root: "unchanged-source"
        self.api._mark_imported = lambda *args, **kwargs: True
        self.api._already_imported = lambda root: True
        self.append_patch = patch("astrbot_host.legacy_contact_configuration.append_private_contact_plan",
                                  side_effect=lambda controller, session, migration, plan, scope: plan)
        self.append_patch.start()
        self.addCleanup(self.append_patch.stop)

    def preview(self):
        return self.api._plan(self.migration, self.root, self.session).unwrap()

    async def apply(self, preview, selected):
        return await self.api._apply(self.migration, self.root, self.session,
                                     preview["plan_token"], selected_fields=selected)

    async def test_selected_field_is_written_and_kept_field_remains_at_its_original_value(self):
        preview = self.preview()
        rows = preview["configuration_review"]["rows"]
        chosen = next(row for row in rows if row["field"] == "memory.context_items")
        self.assertEqual(chosen["before"], 6)
        self.assertTrue(chosen["current_known"])
        result = (await self.apply(preview, [chosen["selection_id"]])).unwrap()
        self.assertEqual(result["written"], ["memory.context_items"])
        self.assertGreater(result["kept_count"], 0)
        after = self.preview()["configuration_review"]["rows"]
        self.assertEqual(next(row for row in after if row["field"] == "memory.context_items")["status"], "same")
        still_default = next(row for row in after if row["field"] == "content.authoring.enabled")
        self.assertNotEqual(still_default["before"], still_default["after"])
        self.assertEqual((self.called, self.sent), ([], []))

    async def test_keeping_every_field_does_not_write_any_setting(self):
        preview = self.preview()
        result = (await self.apply(preview, [])).unwrap()
        self.assertEqual(result["written_count"], 0)
        self.assertEqual(result["kept_count"], preview["change_count"])
        self.assertTrue(result["selection_applied"])
        self.assertEqual(self.preview()["change_count"], preview["change_count"])

    async def test_forged_or_duplicate_identifiers_fail_before_any_write(self):
        preview = self.preview()
        identifier = next(row["selection_id"] for row in preview["configuration_review"]["rows"]
                          if row.get("selection_id"))
        for selection in ([identifier, "forged"], [identifier, identifier]):
            result = await self.apply(preview, selection)
            self.assertEqual(result.failure.code, "legacy_selection_invalid")
            self.assertEqual(self.preview()["change_count"], preview["change_count"])

    async def test_original_plan_cannot_be_reused_after_a_selected_setting_changes(self):
        preview = self.preview()
        identifier = next(row["selection_id"] for row in preview["configuration_review"]["rows"]
                          if row["field"] == "memory.context_items")
        self.assertTrue((await self.apply(preview, [identifier])).is_success)
        again = await self.apply(preview, [identifier])
        self.assertEqual(again.failure.code, "legacy_plan_stale")

    async def test_preview_hides_prompt_bodies_and_credentials_in_both_columns(self):
        preview = self.preview()
        self.assertNotIn("private-instruction-marker", json.dumps(preview))
        self.api.controller.config = {"weather": {"credential": "current-secret-marker"}}
        intent = ConfigIntent("host_options", "weather", {"credential": "old-secret-marker"},
                              "private.host_configuration", target_kind="weather_host_settings")
        rows = configuration_rows(self.api, self.session, [intent], [])
        self.assertNotIn("secret-marker", json.dumps(rows))
        self.assertIn("已配置", rows[0]["before"])
        self.assertIn("已配置", rows[0]["after"])

    async def test_connection_and_identity_combinations_cannot_be_partly_selected(self):
        for intent in (
            ConfigIntent("image.output", "image.output", {"backend": "openai", "endpoints": []},
                         "image.configuration", target_kind="image_output_config"),
            ConfigIntent("plugin_config", "plugin", {"target.account_id": "100", "target.participant_id": "200"},
                         "private.host_configuration", target_kind="plugin_config"),
        ):
            first = selection_id(intent, next(iter(intent.changes)))
            with self.assertRaisesRegex(TaskError, "作为一组"):
                selected_intents(self.api, self.session, [intent], {}, [first])

    async def test_conversion_notes_do_not_hide_effective_values_or_create_fake_confirmations(self):
        rows = configuration_rows(self.api, self.session, [], [
            {"field_id": "role.life.timezone", "kind": "changed_semantics", "summary": "跟随环境"},
            {"field": "role.life.timezone", "kind": "already_present", "before": "Asia/Shanghai",
             "after": "Asia/Shanghai", "explain": "已有相同配置"},
            {"field_id": "retired.setting", "kind": "unmapped", "summary": "当前没有等价设置"},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "same")
        self.assertEqual(rows[0]["after"], "Asia/Shanghai")
        self.assertEqual(rows[1]["status"], "keep")

    async def test_identical_values_do_not_require_confirmation_even_for_a_medium_mapping(self):
        self.api.controller.config = {"models": {"use_streaming": False}}
        intent = ConfigIntent("plugin_config", "plugin", {"models.use_streaming": False},
                              "private.host_configuration", target_kind="plugin_config",
                              confidence={"models.use_streaming": "medium"})
        row = configuration_rows(self.api, self.session, [intent], [])[0]
        self.assertEqual(row["status"], "same")


if __name__ == "__main__":
    unittest.main()
