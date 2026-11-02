"""A reviewed upgrade restores authority without replaying later revocations."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from astrbot_host.legacy_api import LegacyMigrationWebAPI
from companion_v2.identity_policy.legacy_import import authorization_plan, import_authorizations
from companion_v2.identity_policy.models import AuthorizationTier
from companion_v2.identity_policy.object_access import ObjectAccessPolicy
from companion_v2.identity_policy.repository import PersistentIdentityDirectory
from companion_v2.legacy.migration import LegacyMigration
from companion_v2.legacy.private_contacts import private_contacts
from tests.multi_host_support import MultiFixture
from tests.test_legacy_private_contacts import old_user


def source_user(participant="200", **changes):
    values = {"relationship_role": "owner" if participant == "200" else "friend",
        "unified_profile_capabilities": {"schema_version": 1, "grant_source": "administrator",
            "private_companion_enabled": True, "proactive_private_enabled": participant == "200"}}
    values.update(changes)
    return old_user(participant, **values)


class LegacyPrivateAuthorizationTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    async def test_formal_preview_apply_restores_core_and_contact_permissions_without_reimporting_cards(self):
        app = await self.start_host()
        context = self.controller.authority.context("first", "role-a")
        session = NS(profile=NS(target=context), application=app)
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = NS(runtime=app.runtime, now=self.clock.now, data_root=self.root)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = json.dumps({"users": {"200": source_user()}}, ensure_ascii=False) + "\n"
            (root / "companions.json").write_text(source, encoding="utf-8", newline="\n")
            migration = LegacyMigration(v1_root=root, v1_configuration={"basic_config": {"target_user_ids": ["200"]}})
            preview = api._data_plan(migration, root, session).unwrap()
            self.assertEqual(preview["private_contacts"]["authorization"]["core_users"], 1)
            self.assertEqual(preview["private_contacts"]["authorization"]["ready"], 1)
            args = dict(source_group_id="", source_persona_id="", import_reactions=False,
                import_group_network=False, confirm_member_ids=False, import_journals=False,
                confirm_persona_mapping=False, import_worldbook=False, import_private_contacts=True)
            result = api._data_apply_sync(migration, root, session, plan_token=preview["plan_token"], **args).unwrap()
            restored = result["private_contacts"]["authorization"]
            self.assertEqual((restored["identity_restored"], restored["permissions_restored"]), (1, 2))
            tier = app.runtime.read_one("SELECT tier FROM subject_access_tiers WHERE subject_id = ?",
                                       (context.actor.subject.subject_id,))
            self.assertEqual(tier["tier"], "core")
            identity = PersistentIdentityDirectory(app.runtime)
            self.assertTrue(identity.authorize(context.actor, scope=context.scope, capability="proactive.contact",
                observed_at=self.clock.now()).is_success)
            ObjectAccessPolicy(app.runtime).set_tier(context.actor.subject, AuthorizationTier.STANDARD,
                expected_revision=ObjectAccessPolicy(app.runtime).revision, observed_at=self.clock.now())
            grant = next(grant for grant in identity.grants_for_subject(context.actor.subject)
                         if grant.capability == "proactive.contact" and grant.scope == context.scope
                         and grant.state.value == "active")
            identity.revoke_grant(grant.grant_id, revoked_at=self.clock.now(), expected_revision=identity.revision)
            refreshed = api._data_plan(migration, root, session).unwrap()
            again = api._data_apply_sync(migration, root, session, plan_token=refreshed["plan_token"], **args).unwrap()
            self.assertEqual(again["private_contacts"]["authorization"]["identity_restored"], 0)
            self.assertFalse(identity.authorize(context.actor, scope=context.scope, capability="proactive.contact",
                observed_at=self.clock.now()).is_success)
            policy = ObjectAccessPolicy(app.runtime)
            policy.set_tier(context.actor.subject, AuthorizationTier.CORE,
                expected_revision=policy.revision, observed_at=self.clock.now())
            self.assertFalse(policy.feature_access(context.actor, scope=context.scope, action="proactive.contact",
                observed_at=self.clock.now()).is_success, "core identity must preserve the explicit contact revocation")
            self.assertEqual((root / "companions.json").read_text(encoding="utf-8"), source)

    async def test_alias_cannot_become_core_and_pending_permissions_wait_for_registered_private_scope(self):
        app = await self.start_host()
        context = self.controller.authority.context("first", "role-a")
        target = replace(self.controller.settings.target("first"), target_id="friend", participant_id="201")
        actor = self.controller.authority.ensure_actor(target)
        profiles, _ = private_contacts({"users": {"alias": source_user("201", relationship_role="owner")}})
        self.assertIsNone(profiles[0]["authorization"]["tier"])
        plan = authorization_plan(app.runtime, context, profiles, self.clock.now())
        self.assertEqual(plan["deferred"], 1)
        restored = import_authorizations(app.runtime, context, profiles, self.clock.now())
        self.assertEqual(restored["deferred"], 1)
        scope = replace(context.scope, native_scope_id="201", session_id="201")
        self.controller.authority.provision_scope(target, scope, context.role_binding.role)
        identity = PersistentIdentityDirectory(app.runtime)
        self.assertTrue(identity.authorize(actor, scope=scope, capability="conversation.reply",
            observed_at=self.clock.now()).is_success)
        self.assertFalse(identity.authorize(actor, scope=scope, capability="proactive.contact",
            observed_at=self.clock.now()).is_success)
        self.controller.authority.provision_scope(target, scope, context.role_binding.role)
        self.assertFalse(identity.authorize(actor, scope=scope, capability="proactive.contact",
            observed_at=self.clock.now()).is_success)

    async def test_newer_identity_and_permission_decisions_are_preserved_and_do_not_cross_accounts(self):
        app = await self.start_host()
        context = self.controller.authority.context("first", "role-a")
        policy = ObjectAccessPolicy(app.runtime)
        policy.set_tier(context.actor.subject, AuthorizationTier.STANDARD,
            expected_revision=policy.revision, observed_at=self.clock.now())
        identity = PersistentIdentityDirectory(app.runtime)
        identifier = self.controller.authority.grant_id(context.scope, "conversation.reply")
        identity.revoke_grant(identifier, revoked_at=self.clock.now(), expected_revision=identity.revision)
        profiles, _ = private_contacts({"users": {"200": source_user()}})
        other = self.controller.authority.context("second", "role-a")
        self.assertEqual(authorization_plan(app.runtime, other, profiles, self.clock.now())["ready"], 0)
        restored = import_authorizations(app.runtime, context, profiles, self.clock.now())
        self.assertEqual(restored["identity_restored"], 0)
        self.assertGreaterEqual(restored["preserved"], 2)
        self.assertFalse(identity.authorize(context.actor, scope=context.scope, capability="conversation.reply",
            observed_at=self.clock.now()).is_success)


if __name__ == "__main__":
    unittest.main()
