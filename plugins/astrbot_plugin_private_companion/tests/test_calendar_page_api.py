# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import copy
import unittest
from datetime import datetime
from types import SimpleNamespace

from quart import Quart

from astrbot_plugin_private_companion.calendar_contracts import resolve_calendar_snapshot
from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi


class _CalendarPlugin:
    def __init__(self) -> None:
        self.data = {
            "calendar_events": [
                {
                    "kind": "period",
                    "calendar_id": "summer-break",
                    "title": "暑假",
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-31",
                }
            ],
            "calendar_rules": [],
            "calendar_exceptions": [],
            "important_dates": [
                {
                    "id": "anniversary-1",
                    "title": "认识纪念日",
                    "date": "08-20",
                    "repeat_yearly": True,
                    "enabled": True,
                },
                {
                    "id": "disabled-1",
                    "title": "已关闭日期",
                    "date": "08-21",
                    "repeat_yearly": True,
                    "enabled": "false",
                },
                {
                    "id": "one-off-1",
                    "title": "一次性日期",
                    "date": "08-22",
                    "repeat_yearly": "false",
                    "enabled": True,
                },
            ],
        }
        self.calendar_timezone = "Asia/Shanghai"
        self._data_lock = asyncio.Lock()

    def _agenda_now(self) -> datetime:
        return datetime.fromisoformat("2026-08-20T12:00:00+08:00")

    def _agenda_calendar_records_store(self) -> list[dict[str, object]]:
        return [
            copy.deepcopy(item)
            for section in ("calendar_events", "calendar_rules", "calendar_exceptions")
            for item in self.data.get(section, [])
        ]

    def _agenda_calendar_snapshot(self, date_key: str) -> dict[str, object]:
        return resolve_calendar_snapshot(
            self._agenda_calendar_records_store(),
            date_key,
            timezone_name=self.calendar_timezone,
        )

    def _agenda_upsert_calendar_record(self, record: dict[str, object]) -> dict[str, object]:
        kind = str(record.get("kind") or "event")
        section = "calendar_rules" if kind == "recurrence" else "calendar_exceptions" if kind == "exception" else "calendar_events"
        rows = self.data[section]
        item = copy.deepcopy(record)
        item.setdefault("calendar_id", "appointment")
        for index, existing in enumerate(rows):
            if existing.get("calendar_id") == item["calendar_id"]:
                rows[index] = item
                return copy.deepcopy(item)
        rows.append(item)
        return copy.deepcopy(item)

    def _agenda_cancel_calendar_record(self, calendar_id: str) -> bool:
        for section in ("calendar_events", "calendar_rules", "calendar_exceptions"):
            for item in self.data[section]:
                if item.get("calendar_id") == calendar_id:
                    item["status"] = "cancelled"
                    return True
        return False


class CalendarPageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.app = Quart(__name__)
        self.plugin = _CalendarPlugin()
        self.api = PrivateCompanionPageApi(self.plugin)

    async def test_get_calendar_returns_range_records_today_and_conflicts(self) -> None:
        async with self.app.test_request_context("/calendar?month=2026-08"):
            result = await self.api.get_calendar()
        self.assertTrue(result["success"])
        payload = result["data"]
        self.assertEqual(payload["range"], {"start": "2026-08-01", "end": "2026-08-31"})
        self.assertEqual(payload["records"][0]["calendar_id"], "summer-break")
        self.assertEqual(payload["today"]["events"][0]["title"], "暑假")
        legacy = next(item for item in payload["records"] if item.get("read_only"))
        self.assertEqual(legacy["legacy_date"], "08-20")
        self.assertEqual(legacy["kind"], "recurrence")
        self.assertEqual(legacy["title"], "认识纪念日")
        self.assertTrue(any(item.get("title") == "认识纪念日" for item in payload["today"]["events"]))
        self.assertFalse(any(item.get("title") == "已关闭日期" for item in payload["records"]))
        one_off = next(item for item in payload["records"] if item.get("title") == "一次性日期")
        self.assertEqual(one_off["kind"], "event")

    async def test_get_calendar_separates_candidates_and_candidate_confirm_endpoint(self) -> None:
        self.plugin.data["calendar_candidates"] = [
            {
                "candidate_id": "candidate-1",
                "calendar_id": "observed-1",
                "kind": "event",
                "title": "去医院",
                "date": "2026-08-21",
                "start_date": "2026-08-21",
                "lifecycle_state": "candidate",
                "lifecycle": "candidate",
                "status": "tentative",
                "confidence": 0.72,
                "source_excerpt": "明天去医院",
            }
        ]

        def decide(candidate_id, action, **kwargs):
            row = self.plugin.data["calendar_candidates"][0]
            self.assertEqual(candidate_id, "candidate-1")
            self.assertEqual(action, "confirm")
            row["lifecycle_state"] = "confirmed"
            row["lifecycle"] = "confirmed"
            row["status"] = "confirmed"
            return copy.deepcopy(row)

        self.plugin._agenda_decide_calendar_candidate = decide
        async with self.app.test_request_context("/calendar?month=2026-08"):
            result = await self.api.get_calendar()
        self.assertEqual(len(result["data"]["candidates"]), 1)
        self.assertEqual(result["data"]["candidates"][0]["lifecycle_summary"]["lifecycle_state"], "candidate")
        self.assertFalse(any(item.get("calendar_id") == "observed-1" for item in result["data"]["records"]))

        async with self.app.test_request_context(
            "/calendar/candidates/confirm",
            method="POST",
            json={"candidate_id": "candidate-1"},
        ):
            confirmed = await self.api.confirm_calendar_candidate()
        self.assertTrue(confirmed["success"])
        self.assertTrue(confirmed["data"]["confirmed"])

    async def test_preview_does_not_persist_and_cancel_marks_record(self) -> None:
        async with self.app.test_request_context(
            "/calendar/preview",
            method="POST",
            json={
                "kind": "event",
                "calendar_id": "appointment",
                "title": "预约",
                "date": "2026-08-20",
                "start_time": "10:00",
                "end_time": "11:00",
            },
        ):
            preview = await self.api.preview_calendar()
        self.assertTrue(preview["success"])
        self.assertEqual(preview["data"]["record"]["calendar_id"], "appointment")
        self.assertFalse(any(item.get("calendar_id") == "appointment" for item in self.plugin.data["calendar_events"]))

        async with self.app.test_request_context(
            "/calendar/upsert",
            method="POST",
            json={"kind": "event", "calendar_id": "appointment", "title": "预约", "date": "2026-08-20"},
        ):
            saved = await self.api.upsert_calendar()
        self.assertTrue(saved["success"])
        async with self.app.test_request_context(
            "/calendar/cancel",
            method="POST",
            json={"calendar_id": "appointment"},
        ):
            cancelled = await self.api.cancel_calendar()
        self.assertTrue(cancelled["success"])
        self.assertEqual(self.plugin.data["calendar_events"][-1]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
