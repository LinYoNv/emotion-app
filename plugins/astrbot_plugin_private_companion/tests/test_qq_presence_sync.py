# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import unittest

from astrbot_plugin_private_companion.daily_state import DailyStateMixin


class _PresenceHarness(DailyStateMixin):
    enable_qq_presence_sync = True
    enable_qq_custom_presence_sync = False

    def __init__(self, updated_at: float) -> None:
        self.calls = 0
        self.data = {
            "detail_enhanced_day": "2026-07-24",
            "qq_presence_state": {
                "mode": "busy",
                "custom_text": "",
                "updated_at": updated_at,
                "ok": False,
            },
        }

    async def _set_qq_online_presence(self, _mode: str) -> tuple[bool, str]:
        self.calls += 1
        return False, "unsupported action"

    def _save_data_sync(self, **_kwargs) -> None:
        return None


class _CustomPresenceHarness(_PresenceHarness):
    enable_qq_custom_presence_sync = True

    def __init__(self) -> None:
        super().__init__(0)
        self.custom_calls = 0

    async def _set_qq_custom_presence(self, _text: str) -> tuple[bool, str]:
        self.custom_calls += 1
        return False, "unsupported custom action"


class QqPresenceSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_failure_suppresses_same_presence_retry(self) -> None:
        harness = _PresenceHarness(time.time())

        await harness._apply_detail_presence_status(
            {"key": "new-detail"},
            {"presence_status": {"mode": "busy"}},
        )

        self.assertEqual(0, harness.calls)

    async def test_expired_failure_allows_one_presence_retry(self) -> None:
        harness = _PresenceHarness(time.time() - 60 * 60 - 1)

        await harness._apply_detail_presence_status(
            {"key": "new-detail"},
            {"presence_status": {"mode": "busy"}},
        )

        self.assertEqual(1, harness.calls)

    async def test_disabled_custom_sync_does_not_force_online(self) -> None:
        harness = _PresenceHarness(0)

        await harness._apply_detail_presence_status(
            {"key": "custom-detail"},
            {"presence_status": {"mode": "custom", "custom_text": "写题中"}},
        )

        self.assertEqual(0, harness.calls)

    async def test_disabled_custom_sync_does_not_force_online_for_sleep(self) -> None:
        harness = _PresenceHarness(0)

        await harness._apply_detail_presence_status(
            {"key": "sleep-detail"},
            {"presence_status": {"mode": "sleep"}},
        )

        self.assertEqual(0, harness.calls)

    async def test_custom_failure_does_not_fall_back_to_online(self) -> None:
        harness = _CustomPresenceHarness()

        await harness._apply_detail_presence_status(
            {"key": "custom-detail"},
            {"presence_status": {"mode": "custom", "custom_text": "写题中"}},
        )

        self.assertEqual(1, harness.custom_calls)
        self.assertEqual(0, harness.calls)
        self.assertEqual("custom", harness.data["qq_presence_state"]["mode"])
        self.assertFalse(harness.data["qq_presence_state"]["ok"])


if __name__ == "__main__":
    unittest.main()
