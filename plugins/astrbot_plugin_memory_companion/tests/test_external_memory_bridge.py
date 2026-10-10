from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from .package_bootstrap import bootstrap_package
except ImportError:
    from package_bootstrap import bootstrap_package

bootstrap_package()

from astrbot_plugin_memory_companion.core.bridge import MemoryCompanionBridge
from astrbot_plugin_memory_companion.core.classifier import MemoryClassifier
from astrbot_plugin_memory_companion.core.importance import ImportanceEvaluator
from astrbot_plugin_memory_companion.core.service import MemoryCompanionService
from astrbot_plugin_memory_companion.core.store import MemoryStore
from astrbot_plugin_memory_companion.core.visibility import VisibilityPolicy
from astrbot_plugin_memory_companion.core.models import SessionContext


class ExternalMemoryBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.db")
        self.store.initialize()
        self.service = object.__new__(MemoryCompanionService)
        self.service.config = SimpleNamespace(
            bool=lambda key, default=True: default,
        )
        self.service.store = self.store
        self.service.classifier = MemoryClassifier()
        self.service.importance = ImportanceEvaluator()
        self.service._schedule_memory_embedding = lambda *args, **kwargs: None
        self.bridge = MemoryCompanionBridge(self.service)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    async def test_external_memory_is_bound_to_user_private_scope_and_recallable(self) -> None:
        result = await self.bridge.record_external_memory(
            user_id="user-42",
            content="用户开始每周慢跑三次，通常在晚饭后进行。",
            source_plugin="health_app",
            idempotency_key="week-2026-08-25",
            payload={"activity": "running", "frequency": 3},
        )

        self.assertTrue(result["ok"])
        self.assertEqual("stored", result["state"])
        record = await self.store.get_memory(result["memory_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual("private", record.scope)
        self.assertEqual("private_pair", record.visibility)
        self.assertEqual("user-42", record.subject.id)
        self.assertEqual("health_app", record.source_plugin)
        self.assertEqual("external:health_app:user-42", record.session_id)
        visible, reason = VisibilityPolicy().is_visible(
            record,
            SessionContext(scope="private", platform="qq", user_id="user-42", session_id="qq:private:user-42"),
        )
        self.assertTrue(visible, reason)

    async def test_idempotency_key_updates_one_record_instead_of_creating_duplicates(self) -> None:
        first = await self.bridge.record_external_memory(
            user_id="user-7",
            content="用户偏好无糖茶。",
            source_plugin="profile_app",
            idempotency_key="preference:tea",
        )
        second = await self.bridge.record_external_memory(
            user_id="user-7",
            content="用户偏好无糖绿茶。",
            source_plugin="profile_app",
            idempotency_key="preference:tea",
        )

        self.assertEqual(first["memory_id"], second["memory_id"])
        self.assertTrue(second["deduplicated"])
        stored = await self.store.get_memory(first["memory_id"])
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual("用户偏好无糖绿茶。", stored.content)

    async def test_invalid_and_disabled_writes_are_structured(self) -> None:
        missing_user = await self.bridge.record_external_memory(content="没有绑定用户")
        self.assertFalse(missing_user["ok"])
        self.assertEqual("user_id_required", missing_user["error_code"])

        self.service.config = SimpleNamespace(bool=lambda key, default=True: False if key.endswith("accept_external_records") else default)
        disabled = await self.bridge.record_external_memory(user_id="user-1", content="不应写入")
        self.assertFalse(disabled["ok"])
        self.assertEqual("external_records_disabled", disabled["error_code"])

    def test_capability_contract_advertises_external_memory_method(self) -> None:
        snapshot = self.bridge.probe_capability_snapshot()
        self.assertIn("record_external_memory", snapshot["methods"])
