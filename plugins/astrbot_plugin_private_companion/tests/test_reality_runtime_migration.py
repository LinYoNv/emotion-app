from __future__ import annotations

import json
import unittest

from companion_v2.legacy.reality_migration import merge_legacy_runtime_state


def legacy_state():
    return {
        "version": 1,
        "users": {
            "995051631": {
                "user_id": "995051631",
                "reality_touch_camera_consent": {
                    "confirmed": True,
                    "version": 1,
                    "confirmed_at": 1786378243.25,
                    "confirmation_text": "我理解风险并确认授权",
                    "granted_capabilities": ["camera_single_frame"],
                },
                "reality_touch_camera_policy": {
                    "enabled": True,
                    "proactive_mode": "off",
                    "last_observation": {"purpose": "旧测试", "success": True},
                    "audit": [{"purpose": "旧测试"}],
                },
            }
        },
        "reality_touch": {
            "audio_output_device_id": "sd:67e5a2e0d7efa9dc",
            "audio_output_device_name": "扬声器 (小爱音箱-7616)",
            "audio_output_updated_at": 1786378156.46,
            "playback_volume": 100,
            "camera_device_catalog": {"devices": [{"index": 1400, "name": "FHD Webcam"}], "error": ""},
        },
    }


class RealityRuntimeMigrationTests(unittest.TestCase):
    def test_missing_runtime_records_are_copied(self):
        merged, report = merge_legacy_runtime_state({}, legacy_state())
        touch = merged["reality_touch"]
        self.assertEqual(touch["audio_output_device_id"], "sd:67e5a2e0d7efa9dc")
        self.assertEqual(touch["audio_output_device_name"], "扬声器 (小爱音箱-7616)")
        self.assertEqual(touch["playback_volume"], 100)
        self.assertEqual(touch["camera_device_catalog"]["devices"][0]["index"], 1400)
        user = merged["users"]["995051631"]
        self.assertTrue(user["reality_touch_camera_consent"]["confirmed"])
        self.assertEqual(user["reality_touch_camera_consent"]["confirmation_text"], "我理解风险并确认授权")
        self.assertIs(user["reality_touch_camera_policy"]["enabled"], True)
        copied = {item["field"] for item in report if item["kind"] == "copied"}
        self.assertIn("reality.audio_output_device_id", copied)
        self.assertIn("reality.users.995051631.camera_consent", copied)

    def test_current_records_win_and_history_is_not_copied(self):
        current = {
            "users": {"995051631": {"user_id": "995051631",
                                    "reality_touch_camera_consent": {"confirmed": True,
                                                                      "confirmation_text": "陪伴小屋明确确认"},
                                    "reality_touch_camera_policy": {"enabled": False}}},
            "reality_touch": {"audio_output_device_id": "sd:acade3cd80611828",
                              "audio_output_device_name": "扬声器 (小爱音箱-7616)",
                              "playback_volume": 35},
        }
        merged, report = merge_legacy_runtime_state(current, legacy_state())
        touch = merged["reality_touch"]
        self.assertEqual(touch["audio_output_device_id"], "sd:acade3cd80611828")
        self.assertEqual(touch["playback_volume"], 35)
        user = merged["users"]["995051631"]
        self.assertEqual(user["reality_touch_camera_consent"]["confirmation_text"], "陪伴小屋明确确认")
        self.assertIs(user["reality_touch_camera_policy"]["enabled"], False)
        # 旧版的观察与审计属于历史，不应被搬成“刚刚发生”。
        self.assertNotIn("last_observation", user["reality_touch_camera_policy"])
        self.assertNotIn("audit", user["reality_touch_camera_policy"])
        kept = {item["field"] for item in report if item["kind"] == "kept_current"}
        self.assertIn("reality.audio_output_device_id", kept)
        self.assertIn("reality.users.995051631.camera_consent", kept)

    def test_invalid_inputs_leave_state_untouched(self):
        state = {"reality_touch": {"playback_volume": 35}}
        merged, report = merge_legacy_runtime_state(state, None)
        self.assertIs(merged, state)
        self.assertEqual(report, ())
        merged, _ = merge_legacy_runtime_state(state, {"reality_touch": {"playback_volume": 400}})
        self.assertEqual(merged["reality_touch"]["playback_volume"], 35)

    def test_catalog_is_not_confused_with_empty_current_value(self):
        current = {"reality_touch": {"camera_device_catalog": {"devices": [], "error": "no camera"}}}
        merged, report = merge_legacy_runtime_state(current, legacy_state())
        self.assertEqual(merged["reality_touch"]["camera_device_catalog"], {"devices": [], "error": "no camera"})
        self.assertIn("reality.camera_device_catalog",
                      {item["field"] for item in report if item["kind"] == "kept_current"})


if __name__ == "__main__":
    unittest.main()
