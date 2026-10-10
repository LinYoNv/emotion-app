from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "req041_portrait_memory"
if PACKAGE not in sys.modules:
    module = types.ModuleType(PACKAGE)
    module.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = module

from req041_portrait_memory.core.models import SessionContext
from req041_portrait_memory.core.namespace import NamespaceContext
from req041_portrait_memory.core.portrait_namespace import portrait_namespace_decision
from req041_portrait_memory.core.portrait_service import PortraitService
from req041_portrait_memory.core.store import MemoryStore
from req041_portrait_memory.unified_profile_contract import build_profile_dto, build_portrait_request


PERSON_ID = "person_" + "1" * 24
PERSON_REF = {
    "person_id": PERSON_ID,
    "resolved_identity_key": "chat-origin-v1:" + "2" * 64,
    "projection_revision": 1,
    "identity_assurance": "verified",
    "profile_status": "active",
}


class _Config:
    @staticmethod
    def int(_key: str, default: int) -> int:
        return default

    @staticmethod
    def float(_key: str, default: float) -> float:
        return default


def _namespace(*, persona: str, kind: str = "private", group: str = "") -> dict[str, str]:
    return NamespaceContext(
        kind=kind,
        persona_id=persona,
        identity_id=PERSON_ID,
        group_id=group,
        assurance="verified",
        profile_status="active",
        policy_version="req041-v1",
        migration_epoch="epoch-portrait-1",
    ).to_dict()


def _dto(mode: str) -> dict[str, object]:
    return build_profile_dto(
        person_ref=PERSON_REF,
        capability_summary={
            "private_companion_enabled": True,
            "proactive_private_enabled": False,
            "portrait_mode": mode,
            "grant_source": "administrator",
        },
    )


def _request(scope: str, namespace: dict[str, str]) -> dict[str, object]:
    request = build_portrait_request(
        person_ref=PERSON_REF,
        requester_person_id=PERSON_ID,
        target_person_id=PERSON_ID,
        scope=scope,
        purpose="summarize_to_subject",
    )
    request["namespace_context"] = dict(namespace)
    return request


class PortraitNamespaceTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self) -> tuple[MemoryStore, PortraitService]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = MemoryStore(Path(temp.name) / "memory.db")
        store.initialize()
        self.addCleanup(store.close)
        return store, PortraitService(store, _Config())

    async def test_private_capability_survives_disabled_group_observation(self) -> None:
        store, service = self.make_service()
        private_ns = _namespace(persona="persona-a")
        private_event = types.SimpleNamespace(
            private_companion_unified_profile_context=_dto("learn_and_use"),
            private_companion_namespace_context=private_ns,
        )
        captured = await service.capture_user_message(
            SessionContext(
                session_id="onebot:FriendMessage:10001",
                scope="private",
                platform="onebot",
                user_id="10001",
                message_id="private-1",
                message_text="我喜欢烤肉",
            ),
            event=private_event,
        )
        self.assertTrue(captured["ok"])

        group_ns = _namespace(
            persona="persona-a", kind="group_member", group="group-hash-a"
        )
        group_event = types.SimpleNamespace(
            private_companion_unified_profile_context=_dto("disabled"),
            private_companion_namespace_context=group_ns,
        )
        disabled = await service.capture_user_message(
            SessionContext(
                session_id="onebot:GroupMessage:group-a",
                scope="group",
                platform="onebot",
                group_id="group-a",
                user_id="10001",
                message_id="group-1",
                message_text="我喜欢桌游",
            ),
            event=group_event,
        )
        self.assertEqual("portrait_learning_disabled", disabled["code"])

        private_summary = await service.read_summary(_request("private", private_ns))
        group_summary = await service.read_summary(
            _request("group:onebot:group-a", group_ns)
        )
        self.assertTrue(private_summary["ok"])
        self.assertEqual("portrait_usage_disabled", group_summary["code"])
        with store._lock:
            rows = store._conn.execute(
                "SELECT source_scope,capability_summary FROM portrait_scope_capabilities WHERE person_id=?",
                (PERSON_ID,),
            ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual(2, len({row["source_scope"] for row in rows}))

    async def test_persona_capabilities_do_not_overwrite_each_other(self) -> None:
        _store, service = self.make_service()
        enabled_ns = _namespace(persona="persona-a")
        disabled_ns = _namespace(persona="persona-b")
        for namespace, mode, message_id in (
            (enabled_ns, "learn_and_use", "a-1"),
            (disabled_ns, "disabled", "b-1"),
        ):
            await service.capture_user_message(
                SessionContext(
                    session_id=f"onebot:FriendMessage:{message_id}",
                    scope="private",
                    platform="onebot",
                    user_id="10001",
                    message_id=message_id,
                    message_text="我喜欢烤肉",
                ),
                event=types.SimpleNamespace(
                    private_companion_unified_profile_context=_dto(mode),
                    private_companion_namespace_context=namespace,
                ),
            )
        self.assertTrue((await service.read_summary(_request("private", enabled_ns)))["ok"])
        self.assertEqual(
            "portrait_usage_disabled",
            (await service.read_summary(_request("private", disabled_ns)))["code"],
        )

    async def test_cross_scene_low_fact_stays_inside_its_persona(self) -> None:
        _store, service = self.make_service()
        persona_a = _namespace(persona="persona-a")
        persona_b = _namespace(persona="persona-b")
        await service.capture_user_message(
            SessionContext(
                session_id="onebot:FriendMessage:persona-a",
                scope="private",
                platform="onebot",
                user_id="10001",
                message_id="persona-a-food",
                message_text="我喜欢烤肉",
            ),
            event=types.SimpleNamespace(
                private_companion_unified_profile_context=_dto("learn_and_use"),
                private_companion_namespace_context=persona_a,
            ),
        )
        synced_b = await service.sync_profile_context(
            event=types.SimpleNamespace(
                private_companion_unified_profile_context=_dto("use_existing"),
                private_companion_namespace_context=persona_b,
            ),
            legacy_scope="private",
        )
        self.assertTrue(synced_b["ok"])
        self.assertIn("烤肉", str((await service.read_summary(_request("private", persona_a)))["items"]))
        self.assertNotIn("烤肉", str((await service.read_summary(_request("private", persona_b)))["items"]))

    async def test_group_a_and_group_b_source_only_facts_never_cross(self) -> None:
        _store, service = self.make_service()
        namespaces = {
            "group-a": _namespace(
                persona="persona-a", kind="group_member", group="group-hash-a"
            ),
            "group-b": _namespace(
                persona="persona-a", kind="group_member", group="group-hash-b"
            ),
        }
        for group_id, text in (
            ("group-a", "我通常只在A群说苹果暗号"),
            ("group-b", "我通常只在B群说香蕉暗号"),
        ):
            result = await service.capture_user_message(
                SessionContext(
                    session_id=f"onebot:GroupMessage:{group_id}",
                    scope="group",
                    platform="onebot",
                    group_id=group_id,
                    user_id="10001",
                    message_id=f"{group_id}-fact",
                    message_text=text,
                ),
                event=types.SimpleNamespace(
                    private_companion_unified_profile_context=_dto("learn_and_use"),
                    private_companion_namespace_context=namespaces[group_id],
                ),
            )
            self.assertTrue(result["ok"])
        group_a = await service.read_summary(
            _request("group:onebot:group-a", namespaces["group-a"])
        )
        group_b = await service.read_summary(
            _request("group:onebot:group-b", namespaces["group-b"])
        )
        self.assertIn("苹果", str(group_a["items"]))
        self.assertNotIn("香蕉", str(group_a["items"]))
        self.assertIn("香蕉", str(group_b["items"]))
        self.assertNotIn("苹果", str(group_b["items"]))

    async def test_scope_capability_and_fact_survive_store_restart(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "memory.db"
        namespace = _namespace(persona="persona-a")
        first = MemoryStore(path)
        first.initialize()
        service = PortraitService(first, _Config())
        captured = await service.capture_user_message(
            SessionContext(
                session_id="onebot:FriendMessage:restart",
                scope="private",
                platform="onebot",
                user_id="10001",
                message_id="restart-fact",
                message_text="我喜欢烤肉",
            ),
            event=types.SimpleNamespace(
                private_companion_unified_profile_context=_dto("learn_and_use"),
                private_companion_namespace_context=namespace,
            ),
        )
        self.assertTrue(captured["ok"])
        first.close()

        reopened = MemoryStore(path)
        reopened.initialize()
        self.addCleanup(reopened.close)
        summary = await PortraitService(reopened, _Config()).read_summary(
            _request("private", namespace)
        )
        self.assertTrue(summary["ok"])
        self.assertIn("烤肉", str(summary["items"]))

    def test_invalid_or_wrong_identity_namespace_fails_closed(self) -> None:
        valid = _namespace(persona="persona-a")
        exact = portrait_namespace_decision(
            valid, person_id=PERSON_ID, legacy_scope="private", purpose="profile_read"
        )
        self.assertTrue(exact["ok"])
        self.assertTrue(str(exact["source_scope"]).startswith("private@"))
        self.assertNotIn(PERSON_ID, str(exact["source_scope"]))
        wrong = dict(valid)
        wrong["identity_id"] = "person_" + "9" * 24
        self.assertEqual(
            "portrait_namespace_mismatch",
            portrait_namespace_decision(
                wrong, person_id=PERSON_ID, legacy_scope="private", purpose="profile_read"
            )["code"],
        )
        malformed = dict(valid)
        malformed.pop("migration_epoch")
        self.assertEqual(
            "portrait_namespace_invalid",
            portrait_namespace_decision(
                malformed, person_id=PERSON_ID, legacy_scope="private", purpose="profile_read"
            )["code"],
        )
        self.assertEqual(
            "portrait_namespace_invalid",
            portrait_namespace_decision(
                None,
                person_id=PERSON_ID,
                legacy_scope="private",
                purpose="profile_read",
                namespace_present=True,
            )["code"],
        )

    def test_missing_namespace_keeps_explicit_legacy_compatibility_only(self) -> None:
        decision = portrait_namespace_decision(
            None,
            person_id=PERSON_ID,
            legacy_scope="group:onebot:group-a",
            purpose="profile_read",
        )
        self.assertEqual("legacy", decision["state"])
        self.assertEqual("group:onebot:group-a", decision["source_scope"])


if __name__ == "__main__":
    unittest.main()
