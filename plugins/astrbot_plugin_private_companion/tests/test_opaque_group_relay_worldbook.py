# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_private_companion.atrelay import AtRelayMixin
from astrbot_plugin_private_companion.core_store import CoreStoreMixin
from astrbot_plugin_private_companion.llm_tool_actions import LlmToolActionsMixin
from astrbot_plugin_private_companion.worldbook import WorldbookMixin


OPAQUE_GROUP_ID = "8EC9DA9653F094D2D9CC640B6EC225C0"
SECOND_GROUP_ID = "group-openid-2"
GROUP_UMO = f"QBot4012710235:GroupMessage:{OPAQUE_GROUP_ID}"


class _RelayHarness(LlmToolActionsMixin, AtRelayMixin):
    enable_atrelay_tools = True
    atrelay_multi_target_limit = 8

    def __init__(self) -> None:
        self.data = {
            "groups": {
                OPAQUE_GROUP_ID: {
                    "group_id": OPAQUE_GROUP_ID,
                    "name": "官方测试群",
                }
            },
            "worldbook_group_profiles": {},
        }
        self._data_lock = asyncio.Lock()
        self._save_data_sync = Mock()
        self._send_atrelay_chain_to_target = AsyncMock(
            return_value=(True, "", GROUP_UMO)
        )

    _normalize_group_identity_id = staticmethod(
        CoreStoreMixin._normalize_group_identity_id
    )

    @staticmethod
    def _configured_group_ids() -> list[str]:
        return []

    @staticmethod
    def _extract_group_id_from_event(_event) -> str:
        return ""

    @staticmethod
    def _atrelay_boundary_guard(_text) -> str:
        return ""

    @staticmethod
    def _atrelay_tool_authorization(_event) -> tuple[bool, str]:
        return True, "owner"

    @staticmethod
    def _atrelay_confirmation_guard(*_args, **_kwargs) -> str:
        return ""

    @staticmethod
    def _atrelay_event_confirms_sensitive_send(_event) -> bool:
        return False

    @staticmethod
    def _atrelay_source_snapshot_for_event(_event) -> tuple[str, str]:
        return "source-user", "来源用户"

    async def _resolve_atrelay_target_user(self, _event, _group_id, _hint):
        return {"user_id": "123456", "name": "目标群友"}

    def _get_group(self, group_id: str) -> dict:
        return self.data["groups"].setdefault(
            group_id,
            {"group_id": group_id},
        )


class _WorldbookHarness(WorldbookMixin):
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self.data = {
            "worldbook_deleted_member_ids": [],
            "worldbook_deleted_group_ids": [],
            "worldbook_member_profiles": {},
            "worldbook_group_profiles": {},
        }

    _normalize_group_identity_id = staticmethod(
        CoreStoreMixin._normalize_group_identity_id
    )

    def _worldbook_config_path_candidates(self) -> list[Path]:
        return [self._config_path]


class OpaqueGroupRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_group_tool_accepts_opaque_group_id(self) -> None:
        harness = _RelayHarness()

        result = await harness._pc_send_to_group_impl(
            SimpleNamespace(),
            group_id=OPAQUE_GROUP_ID,
            message="你好",
        )

        self.assertIn(f"群 {OPAQUE_GROUP_ID}", result)
        self.assertEqual(
            OPAQUE_GROUP_ID,
            harness._send_atrelay_chain_to_target.await_args.kwargs["target_id"],
        )

    async def test_multi_group_tool_normalizes_umo_and_opaque_ids(self) -> None:
        harness = _RelayHarness()
        harness._pc_send_to_group_impl = AsyncMock(return_value="消息已发送")

        result = await harness._pc_send_to_groups_impl(
            SimpleNamespace(),
            group_ids=[GROUP_UMO, SECOND_GROUP_ID],
            message="群发通知",
        )

        self.assertIn("多群通知完成", result)
        sent_ids = [
            call.kwargs["group_id"]
            for call in harness._pc_send_to_group_impl.await_args_list
        ]
        self.assertEqual([OPAQUE_GROUP_ID, SECOND_GROUP_ID], sent_ids)

    async def test_scheduled_group_tool_normalizes_full_umo(self) -> None:
        harness = _RelayHarness()

        result = await harness._pc_schedule_group_relay_impl(
            SimpleNamespace(),
            group_id=GROUP_UMO,
            at_user="目标群友",
            message="看到后告诉我",
        )

        self.assertIn(f"群 {OPAQUE_GROUP_ID}", result)
        tasks = harness.data["groups"][OPAQUE_GROUP_ID]["pending_atrelay_tasks"]
        self.assertEqual("123456", tasks[0]["target_user_id"])

    async def test_group_hint_only_treats_known_opaque_id_or_umo_as_direct(self) -> None:
        harness = _RelayHarness()
        event = SimpleNamespace(bot=None)

        known = await harness._resolve_atrelay_target_group(event, OPAQUE_GROUP_ID)
        umo = await harness._resolve_atrelay_target_group(event, GROUP_UMO)
        by_name = await harness._resolve_atrelay_target_group(event, "官方测试群")
        unknown_name = await harness._resolve_atrelay_target_group(event, "随便一个群名")

        self.assertEqual(
            {"status": "success", "group_id": OPAQUE_GROUP_ID, "source": "direct"},
            known,
        )
        self.assertEqual(known, umo)
        self.assertEqual(OPAQUE_GROUP_ID, by_name["group_id"])
        self.assertEqual("plugin_group", by_name["source"])
        self.assertEqual("need_group", unknown_name["status"])


class OpaqueGroupWorldbookTests(unittest.TestCase):
    def test_group_scope_accepts_umo_but_user_scope_remains_numeric(self) -> None:
        payload = {
            "entry_storage": [
                {
                    "__template_key": "group",
                    "name": "官方关系群",
                    "content": "这是群组知识。",
                    "scope": [GROUP_UMO, SECOND_GROUP_ID],
                },
                {
                    "__template_key": "user",
                    "name": "用户资料",
                    "content": "这是用户知识。",
                    "scope": ["opaque-user-id", "123456"],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "关系网.json"
            config_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            harness = _WorldbookHarness(config_path)

            self.assertTrue(harness._import_worldbook_entries_from_sources())

        self.assertEqual(
            {OPAQUE_GROUP_ID, SECOND_GROUP_ID},
            set(harness.data["worldbook_group_profiles"]),
        )
        self.assertEqual(
            {"123456"},
            set(harness.data["worldbook_member_profiles"]),
        )
        normalized_entry = harness.data["worldbook_entries"][0]
        self.assertIn(GROUP_UMO, normalized_entry["scope"])


if __name__ == "__main__":
    unittest.main()
