# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from astrbot.api.message_components import Plain

from astrbot_plugin_private_companion.final_response_persistence import (
    FinalResponsePersistenceMixin,
)
from astrbot_plugin_private_companion.group_observation import GroupObservationMixin


class _Event:
    def __init__(self, results: list[object]) -> None:
        self.unified_msg_origin = "default:GroupMessage:group-a"
        self._has_send_oper = True
        self._private_companion_persistence_managed = True
        self._private_companion_llm_planned_chunk_texts = (
            "第一段",
            "第二段",
            "第三句",
        )
        self._private_companion_llm_planned_segment_ids = (0, 1, 1)
        self._private_companion_official_assistant_message = object()
        self.private_companion_group_scene = {"talking_to": "group"}
        self._results = iter(results)
        self.sent: list[object] = []

    def get_result(self):
        return SimpleNamespace(chain=[Plain("候选回复")])

    def get_sender_id(self) -> str:
        return "user-a"

    def is_private_chat(self) -> bool:
        return False

    def is_stopped(self) -> bool:
        return False

    async def send(self, message):
        self.sent.append(message)
        return next(self._results)


class _Harness(FinalResponsePersistenceMixin, GroupObservationMixin):
    max_group_recent_messages = 8

    def __init__(self) -> None:
        self._data_lock = asyncio.Lock()
        self.group = {"group_id": "group-a"}
        self.saved_sections: set[str] = set()

    @staticmethod
    def _extract_group_id_from_event(_event) -> str:
        return "group-a"

    def _get_group(self, _group_id: str) -> dict:
        return self.group

    @staticmethod
    def _feature_enabled_or_temp_unlocked(_key: str) -> bool:
        return True

    def _save_data_sync(self, *, sections=None) -> None:
        self.saved_sections.update(sections or ())

    @staticmethod
    def _event_message_id(_event) -> str:
        return "message-group-1"

    @staticmethod
    def _stage_delivered_assistant_for_official_history(**_kwargs) -> bool:
        return False

    async def _append_delivered_assistant_to_conversation(self, **_kwargs) -> bool:
        return False

    async def _record_final_assistant_in_livingmemory(self, **_kwargs) -> bool:
        return False


class GroupBotHistoryDeliveryE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_segmented_send_persists_group_segment_metadata(self) -> None:
        harness = _Harness()
        event = _Event([True, True, True])
        harness._capture_final_outbound_delivery(event)
        harness._final_response_persistence_coordinator().mark_final_response_ready(event)

        await event.send(SimpleNamespace(chain=[Plain("第一段")]))
        await event.send(SimpleNamespace(chain=[Plain("第二段")]))
        await event.send(SimpleNamespace(chain=[Plain("第三句")]))
        self.assertTrue(await harness._persist_final_outbound_delivery(event))

        record = harness.group["recent_bot_replies"][-1]
        self.assertEqual("第一段 第二段 第三句", record["text"])
        self.assertEqual(["第一段", "第二段第三句"], record["llm_segments"])
        self.assertEqual("default:GroupMessage:group-a", event.unified_msg_origin)
        self.assertEqual({"groups"}, harness.saved_sections)

    async def test_partial_segmented_send_persists_only_confirmed_logical_content(self) -> None:
        harness = _Harness()
        event = _Event([True, False, True])
        harness._capture_final_outbound_delivery(event)
        harness._final_response_persistence_coordinator().mark_final_response_ready(event)

        await event.send(SimpleNamespace(chain=[Plain("第一段")]))
        self.assertFalse(await event.send(SimpleNamespace(chain=[Plain("第二段")])))
        await event.send(SimpleNamespace(chain=[Plain("第三句")]))
        self.assertTrue(await harness._persist_final_outbound_delivery(event))

        record = harness.group["recent_bot_replies"][-1]
        self.assertEqual("第一段 第三句", record["text"])
        self.assertEqual(["第一段", "第三句"], record["llm_segments"])


if __name__ == "__main__":
    unittest.main()
