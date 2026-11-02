"""Private users come from reviewed plugin records, never the native session list."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from astrbot_host.legacy_api import LegacyMigrationWebAPI
from companion_v2.legacy.migration import LegacyMigration
from companion_v2.legacy.private_contacts import private_contacts
from companion_v2.memory.person.legacy_import import contact_plan, import_contacts
from companion_v2.memory.person.management import PersonAcquaintanceDirectory
from tests.multi_host_support import MultiFixture


def old_user(participant="200", **changes):
    return {"identity_subject_id": participant, "identity_adapter_instance_id": "qq-main",
        "identity_bot_id": "100", "identity_platform_kind": "onebot",
        "umo": f"qq-main:FriendMessage:{participant}", "private_inbound_count": 12,
        "manual_enabled": True, "nickname": "旧称呼", **changes}


class PrivateContactConversionTests(unittest.TestCase):
    def test_real_private_users_deduplicate_and_group_and_empty_routes_stay_out(self):
        contacts, rejected = private_contacts({"users": {
            "200": old_user(), "alias": old_user(private_inbound_count=3),
            "201": old_user("201", manual_enabled=False, private_inbound_count=2),
            "202": old_user("202", manual_enabled=False, private_inbound_count=0),
            "group": old_user("203", observation_only=True),
            "name-only": {"nickname": "旧称呼", "private_inbound_count": 9},
        }})
        self.assertEqual([(p["native_account_id"], p["message_count"]) for p in contacts],
                         [("200", 12), ("201", 2)])
        self.assertEqual({r["reason"] for r in rejected},
            {"no_private_history", "group_observation", "private_identity_unverified"})
        self.assertTrue(all(p["last_seen"] is None for p in contacts))


class PrivateContactMigrationTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    async def test_official_data_plan_and_apply_restore_directory_idempotently(self):
        app = await self.start_host()
        context = self.controller.session.authority.context("first", "role-a")
        session = NS(profile=NS(target=context), application=app)
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = NS(runtime=self.controller.runtime, now=self.clock.now, data_root=self.root)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = json.dumps({"users": {"200": old_user(),
                "other-account": old_user("999", identity_bot_id="101"),
                "unregistered": old_user("777")}}, ensure_ascii=False) + "\n"
            (root / "companions.json").write_text(source, encoding="utf-8", newline="\n")
            migration = LegacyMigration(v1_root=root)
            preview = api._data_plan(migration, root, session).unwrap()
            self.assertEqual(preview["private_contacts"]["ready"], 1)
            self.assertEqual(len(preview["private_contacts"]["pending"]), 1)
            self.assertEqual(len(preview["private_contacts"]["excluded"]), 1)
            access_before = [dict(row) for row in app.runtime.read_all("SELECT * FROM subject_access_tiers", ())]
            kwargs = dict(source_group_id="", source_persona_id="", import_reactions=False,
                import_group_network=False, confirm_member_ids=False, import_journals=False,
                confirm_persona_mapping=False, import_worldbook=False, import_private_contacts=True)
            result = api._data_apply_sync(migration, root, session,
                plan_token=preview["plan_token"], **kwargs).unwrap()
            self.assertEqual(result["private_contacts"]["created"], 1)
            route = {"platform": context.scope.platform, "bot_id": context.scope.bot_id,
                "account_id": context.scope.account_id, "role_id": context.role_binding.role.subject_id}
            directory = PersonAcquaintanceDirectory(app.runtime).directory([route])
            self.assertEqual(directory["counts"]["private_chat"], 1)
            person = directory["persons"][0]
            self.assertEqual((person["display_name"], person["private"]["msg_count"]), ("旧称呼", 12))
            self.assertTrue(person["legacy_private_contact"])
            self.assertEqual(app.runtime.read_one("SELECT COUNT(*) AS n FROM conversation_inputs", ())["n"], 0)
            self.assertEqual([dict(row) for row in app.runtime.read_all("SELECT * FROM subject_access_tiers", ())], access_before)
            refreshed = api._data_plan(migration, root, session).unwrap()
            self.assertEqual(refreshed["private_contacts"]["already_present"], 1)
            again = api._data_apply_sync(migration, root, session,
                plan_token=refreshed["plan_token"], **kwargs).unwrap()
            self.assertEqual(again["private_contacts"]["created"], 0)
            self.assertEqual((root / "companions.json").read_text(encoding="utf-8"), source)

    async def test_current_card_names_and_counters_survive_and_roles_remain_separate(self):
        app = await self.start_host()
        context = self.controller.session.authority.context("first", "role-a")
        app.messages.acquaintance.record_platform_names(context, nickname="当前昵称")
        from companion_v2.execution.models import encode
        from companion_v2.memory.person.access import owner_key, card_id
        with app.runtime.transaction() as connection:
            identifier = card_id(owner_key(context))
            row = connection.execute("SELECT document_json FROM memory_views WHERE view_id = ?", (identifier,)).fetchone()
            document = json.loads(row["document_json"])
            document["admin_nickname"] = "当前备注"
            document["contexts"]["private"] = {"msg_count": 3, "active_days": 1,
                "first_seen": "2026-10-24T00:00:00+00:00", "last_seen": "2026-10-24T00:00:00+00:00"}
            connection.execute("UPDATE memory_views SET document_json = ? WHERE view_id = ?", (encode(document), identifier))
        profiles, _ = private_contacts({"users": {"200": old_user()}})
        result = import_contacts(app.runtime, context, profiles, self.clock.now())
        self.assertEqual(result["created"], 1)
        row = app.runtime.read_one("SELECT document_json FROM memory_views WHERE view_id = ?", (identifier,))
        restored = json.loads(row["document_json"])
        self.assertEqual(restored["admin_nickname"], "当前备注")
        self.assertEqual(restored["contexts"]["private"]["msg_count"], 3)
        person = PersonAcquaintanceDirectory(app.runtime)._person(restored["route"], restored, None, 1)
        self.assertEqual((person["display_name"], person["private"]["msg_count"]), ("当前备注", 15))
        other_role = self.controller.session.authority.context("first", "role-b")
        self.assertEqual(contact_plan(app.runtime, other_role, profiles)["ready"], 1)
        other_account = self.controller.session.authority.context("second", "role-a")
        self.assertEqual(contact_plan(app.runtime, other_account, profiles)["ready"], 0)
