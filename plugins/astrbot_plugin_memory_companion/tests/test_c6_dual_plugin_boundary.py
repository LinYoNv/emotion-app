from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import bot_personal_contract
from core.bridge import MemoryCompanionBridge
from core.capability_probe import CapabilityCache, build_capability_snapshot
from core.models import MemoryRecord
from core.service import MemoryCompanionService
from core.store import MemoryStore


def run(coro):
    return asyncio.run(coro)


def archive(index: str = "1", **overrides):
    value = {
        "memory_type": "bot_observed_activity",
        "memory_domain": bot_personal_contract.BOT_PERSONAL_MEMORY_DOMAIN,
        "subject": "bot_self",
        "date": "2026-07-30",
        "window": "afternoon",
        "occurred_at": "2026-07-30T15:00:00+08:00",
        "source_kind": "observed",
        "source_refs": [f"companion:event:{index}"],
        "certainty": 0.9,
        "evidence_level": "L2",
        "status": "active",
        "version": 1,
        "idempotency_key": f"c6:test:{index}",
        "payload_schema_version": bot_personal_contract.BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION,
        "payload": {"summary": "safe archive summary", "activity": "rest"},
    }
    value.update(overrides)
    return value


class _Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "record_id": "c6-record", "version": 1}

    async def record_external_event(self, **kwargs):
        self.calls.append(kwargs)
        return "external-record"

    async def record_bot_personal_archive(self, dto):
        self.calls.append(dto)
        return self.result


class _ProfileProvider:
    async def read_bot_personal_profile(self, **_kwargs):
        return {
            "ok": True,
            "state": "ready",
            "items": [
                {
                    "record_id": "bot-1",
                    "memory_domain": bot_personal_contract.BOT_PERSONAL_MEMORY_DOMAIN,
                    "memory_type": "bot_observed_activity",
                    "summary": "safe summary",
                    "payload": {"secret": "must not cross"},
                    "content": "raw content must not cross",
                }
            ],
        }


class C6DualPluginBoundaryTests(unittest.TestCase):
    def test_private_companion_registration_accepts_exact_class_and_instance_forms(self):
        """AstrBot exposes both the live Star and its exact class in metadata."""

        class Companion:
            pass

        live = Companion()
        plugin = SimpleNamespace(
            context=SimpleNamespace(
                get_all_stars=lambda: [
                    SimpleNamespace(
                        star_cls=live,
                        star_cls_type=Companion,
                        root_dir_name="astrbot_plugin_private_companion",
                        name="PrivateCompanion",
                        activated=True,
                    )
                ]
            )
        )
        bridge = MemoryCompanionBridge(plugin)

        self.assertIsNotNone(bridge.register_emotion_producer(Companion))
        self.assertIsNotNone(bridge.register_emotion_producer(live))

        # A same-name class or another instance must not inherit the
        # capability merely from metadata labels.
        SameName = type("Companion", (), {})
        self.assertIsNone(bridge.register_emotion_producer(SameName))
        self.assertIsNone(bridge.register_emotion_producer(Companion()))

    def test_capability_probe_exposes_contract_domain_version_types_and_pending(self):
        bridge = MemoryCompanionBridge(object())
        pending = bridge.capability_status()
        self.assertEqual("unprobed", pending["capability_state"])
        self.assertTrue(pending["pending"])
        self.assertFalse(pending["available"])

        snapshot = bridge.probe_capability_snapshot()
        self.assertEqual("available", snapshot["capability_state"])
        self.assertTrue(snapshot["available"])
        self.assertFalse(snapshot["degraded"])
        self.assertEqual(bot_personal_contract.BOT_PERSONAL_MEMORY_DOMAIN, snapshot["domain"])
        self.assertEqual(str(bot_personal_contract.CONTRACT_REVISION), snapshot["contract_version"])
        self.assertEqual(
            list(bot_personal_contract.BOT_PERSONAL_MEMORY_TYPES), snapshot["memory_types"]
        )
        self.assertEqual(list(bot_personal_contract.WINDOW_SLUGS), snapshot["windows"])
        self.assertIn("record_bot_personal_archive", snapshot["methods"])

    def test_missing_contract_metadata_is_bounded_and_degraded(self):
        snapshot = build_capability_snapshot(
            contract_module=object(), state="degraded", warnings=["contract_missing"], error_code="bridge_missing"
        )
        self.assertEqual("degraded", snapshot["state"])
        self.assertTrue(snapshot["degraded"])
        self.assertFalse(snapshot["pending"])
        self.assertEqual("bridge_missing", snapshot["error_code"])
        self.assertEqual([], snapshot["domains"])
        self.assertEqual([], snapshot["memory_types"])
        self.assertEqual(["contract_missing"], snapshot["warnings"])
        self.assertTrue(all(isinstance(value, (str, bool, list, dict)) for value in snapshot.values()))

    def test_compatibility_writes_keep_bot_user_and_group_contexts_separate(self):
        recorder = _Recorder()
        bridge = MemoryCompanionBridge(recorder)

        run(bridge.record_creative_work(content="C6 creative", session_id="bot:creative"))
        run(
            bridge.record_event(
                content="user fact",
                memory_type="user_fact",
                scope="private",
                session_id="private:user-1",
                visibility="private_pair",
                reality_level="real_user_fact",
                subject={"kind": "user", "id": "user-1", "role": "current_sender"},
            )
        )
        run(
            bridge.record_event(
                content="group fact",
                memory_type="group_memory",
                scope="group",
                session_id="group:42",
                group_id="42",
                visibility="group_public",
                reality_level="observed_utterance",
                subject={"kind": "user", "id": "user-2", "role": "group_member"},
            )
        )

        creative, user, group = recorder.calls
        self.assertEqual(("creative_work", "bot_self", "fictional_content"),
                         (creative["memory_type"], creative["visibility"], creative["reality_level"]))
        self.assertEqual(("private", "private:user-1", ""),
                         (user["scope"], user["session_id"], user["group_id"]))
        self.assertEqual(("group", "group:42", "42"),
                         (group["scope"], group["session_id"], group["group_id"]))
        self.assertNotEqual(creative["visibility"], user["visibility"])
        self.assertNotEqual(user["scope"], group["scope"])
        self.assertNotEqual(user["session_id"], group["session_id"])

    def test_entertainment_compatibility_writes_remain_bot_personal_records(self):
        recorder = _Recorder()
        bridge = MemoryCompanionBridge(recorder)

        run(bridge.record_search_action(content="news summary", session_id="bot:news"))
        run(bridge.record_image_action(content="image summary", session_id="bot:image"))
        run(bridge.record_qzone_action(content="qzone summary", session_id="bot:qzone"))
        run(bridge.record_reading(content="reading summary", session_id="bot:reading"))
        run(bridge.record_schedule_fragment(content="outfit summary", session_id="bot:outfit"))

        self.assertEqual(
            ["search_action", "image_action", "qzone_action", "reading_memory", "schedule_fragment"],
            [item["memory_type"] for item in recorder.calls],
        )
        self.assertTrue(all(item["visibility"] == "bot_self" for item in recorder.calls))
        self.assertEqual(
            ["bot_action", "bot_action", "bot_action", "bot_action", "persona_life"],
            [item["reality_level"] for item in recorder.calls],
        )
        self.assertTrue(all(item["source_plugin"] == "external" for item in recorder.calls))

    def test_bot_personal_archive_is_read_only_and_isolated_from_user_and_group_records(self):
        with tempfile.TemporaryDirectory() as directory:
            service = object.__new__(MemoryCompanionService)
            service.store = MemoryStore(Path(directory) / "memory.db")
            service.store.initialize()
            service._schedule_memory_embedding = lambda *args, **kwargs: None
            try:
                bot = run(service.record_bot_personal_archive(archive("bot")))
                run(service.store.insert_memory(MemoryRecord(
                    id="user-1", memory_type="user_fact", scope="private", session_id="private:user",
                    visibility="private_pair", content="user fact", metadata={"memory_domain": "user_memory"},
                )))
                run(service.store.insert_memory(MemoryRecord(
                    id="group-1", memory_type="group_memory", scope="group", session_id="group:42",
                    group_id="42", visibility="group_public", content="group fact",
                    metadata={"memory_domain": "group_memory"},
                )))
                profile = run(service.read_bot_personal_profile(limit=10))
                self.assertTrue(profile["read_only"])
                self.assertEqual([bot["record_id"]], [item["record_id"] for item in profile["items"]])
                self.assertTrue(all(item["memory_domain"] == bot_personal_contract.BOT_PERSONAL_MEMORY_DOMAIN
                                    for item in profile["items"]))
                self.assertNotIn("payload", profile["items"][0])
                self.assertNotIn("content", profile["items"][0])
            finally:
                service.store.close()

    def test_bridge_failure_states_are_identifiable_without_leaking_exception_text(self):
        missing = run(MemoryCompanionBridge(object()).record_bot_personal_archive(archive()))
        self.assertEqual(("forbidden", "producer_capability_required"),
                         (missing["state"], missing["error_code"]))

        def attested(provider):
            companion = object()
            provider.context = SimpleNamespace(
                get_all_stars=lambda: [
                    SimpleNamespace(
                        star_cls=companion,
                        root_dir_name="astrbot_plugin_private_companion",
                        name="PrivateCompanion",
                        activated=True,
                    )
                ]
            )
            bridge = MemoryCompanionBridge(provider)
            return bridge, bridge.register_emotion_producer(companion)

        class Broken:
            async def record_bot_personal_archive(self, _dto):
                raise asyncio.TimeoutError()

        timed_out_bridge, timed_out_capability = attested(Broken())
        timed_out = run(
            timed_out_bridge.record_bot_personal_archive(
                archive("timeout"), producer_capability=timed_out_capability
            )
        )
        self.assertEqual(("degraded", "bridge_exception"),
                         (timed_out["state"], timed_out["error_code"]))
        self.assertNotIn("TimeoutError", str(timed_out))

        failed = _Recorder({"ok": False, "state": "failed", "error_code": "archive_failed"})
        failed_bridge, failed_capability = attested(failed)
        failed_result = run(
            failed_bridge.record_bot_personal_archive(
                archive("failed"), producer_capability=failed_capability
            )
        )
        self.assertEqual(("failed", "archive_failed"),
                         (failed_result["state"], failed_result["error_code"]))

    def test_profile_bridge_preserves_pending_degraded_and_filters_archive_payload(self):
        ready = run(MemoryCompanionBridge(_ProfileProvider()).read_bot_personal_profile())
        self.assertTrue(ready["read_only"])
        self.assertEqual("ready", ready["state"])
        self.assertEqual(["bot-1"], [item["record_id"] for item in ready["items"]])
        self.assertNotIn("payload", ready["items"][0])
        self.assertNotIn("content", ready["items"][0])

        unavailable = run(MemoryCompanionBridge(object()).read_bot_personal_profile())
        self.assertEqual("degraded", unavailable["state"])
        self.assertTrue(unavailable["pending"])
        self.assertEqual("bridge_method_unavailable", unavailable["error_code"])

    def test_capability_probe_failure_keeps_degraded_contract_fields(self):
        with patch.object(bot_personal_contract, "capability_descriptor", side_effect=RuntimeError("boom")):
            result = MemoryCompanionBridge(object()).probe_capability_snapshot()
        self.assertEqual("negative", result["capability_state"])
        self.assertTrue(result["degraded"])
        self.assertFalse(result["available"])
        self.assertEqual("contract_descriptor_exception", result["error_code"])
        self.assertEqual(bot_personal_contract.BOT_PERSONAL_MEMORY_DOMAIN, result["domain"])
        self.assertIn("contract_descriptor_exception", result["warnings"])


if __name__ == "__main__":
    unittest.main()
