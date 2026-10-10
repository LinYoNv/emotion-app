# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time

from datetime import datetime, timezone

from astrbot_plugin_private_companion.place_cognitive_map import PlaceCognitiveMapMixin
from astrbot_plugin_private_companion.proactive import ProactiveMixin
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin
from astrbot_plugin_private_companion.scene_context import SceneContextMixin


class MobileSceneHarness(PlaceCognitiveMapMixin, SceneContextMixin):
    def __init__(self) -> None:
        self.data = {
            "daily_state": {"date": "2026-08-12", "energy": 70, "mood_bias": "平稳"},
            "daily_plan": {"date": "2026-08-12", "items": []},
        }

        self.enable_weather_context = False
        self.enable_weather_alerts = False
        self.events: list[dict] = []

    def _schedule_data_save(self, **_kwargs) -> None:
        return None

    def _place_cognitive_map_emit_memory_event(self, event: dict) -> None:
        self.events.append(event)

    @staticmethod
    def _scene_context_now() -> datetime:
        return datetime(2026, 8, 12, 15, 30, tzinfo=timezone.utc)

    @staticmethod
    def _reality_mobile_context(user_id: str):
        return {
            "available": user_id in {"owner-1", "owner-unmatched", "owner-home"},
            "location": {
                "available": user_id in {"owner-1", "owner-unmatched", "owner-home"},
                "latitude": 31.23,
                "longitude": 121.474,
                "accuracy_m": 24.0,
                "label": "外出中",
                "place": {
                    "matched": user_id in {"owner-1", "owner-home"},
                    "name": "公司" if user_id in {"owner-1", "owner-unmatched"} else "家",
                    "kind": "work" if user_id in {"owner-1", "owner-unmatched"} else "home",
                    "distance_m": 18 if user_id in {"owner-1", "owner-home"} else 0,
                    "radius_m": 150 if user_id in {"owner-1", "owner-home"} else 0,
                },
            },
            "device": {
                "available": user_id == "owner-1",
                "app_state": "foreground",
                "battery_percent": 78,
                "charging": True,
                "stale": False,
            },
        }


def test_mobile_telemetry_is_added_as_non_diagnostic_companion_context() -> None:
    harness = MobileSceneHarness()
    context = MobileSceneHarness._reality_mobile_context("owner-1")
    context["telemetry"] = {
        "available": True,
        "summary": "心率 78 bpm；当前活动：步行（约 18 分钟）",
    }
    harness._reality_mobile_context = lambda _user_id: context

    formatted = harness._format_mobile_user_location_context({"user_id": "owner-1"})

    assert "心率 78 bpm" in formatted
    assert "不是医疗结论" in formatted
    assert "不得据此诊断" in formatted


class ProactiveLocationHarness(ProactiveMixin, ProactiveEngineMixin, PlaceCognitiveMapMixin, SceneContextMixin):
    def __init__(self) -> None:
        self.data = {
            "daily_state": {"energy": 70},
            "daily_plan": {"items": []},
            "daily_weather": {"prompt": "刚下雨"},
        }

    _reality_mobile_context = staticmethod(MobileSceneHarness._reality_mobile_context)

    @staticmethod
    def _weather_summary_text(_weather) -> str:
        return "刚下雨"

    @staticmethod
    def _ordinary_weather_topic_available(_user) -> bool:
        return True

    @staticmethod
    def _proactive_current_agenda_item():
        return None

    @staticmethod
    def _current_story_plan_snapshot():
        return {}

    @staticmethod
    def _private_user_role(_user):
        return "primary"


class ProactiveTransitionHarness(ProactiveLocationHarness):
    def __init__(self) -> None:
        super().__init__()
        self.current_place: tuple[str, str] | None = ("公司", "work")
        self.data["users"] = {"owner-route": {"user_id": "owner-route", "enabled": True}}
        self.kick_count = 0

    def _reality_mobile_context(self, _user_id: str):
        if self.current_place is None:
            return {
                "available": True,
                "location": {
                    "available": True,
                    "latitude": 31.24,
                    "longitude": 121.48,
                    "place": {"matched": False, "name": "公司", "kind": "work"},
                },
            }
        name, kind = self.current_place
        return {
            "available": True,
            "location": {
                "available": True,
                "latitude": 31.23,
                "longitude": 121.474,
                "place": {
                    "matched": True,
                    "name": name,
                    "kind": kind,
                    "distance_m": 18,
                    "radius_m": 150,
                },
            },
        }

    @staticmethod
    def _reality_companion_api():
        return object()

    @staticmethod
    def _relationship_owner_user_ids():
        return {"owner-route"}

    @staticmethod
    def _configured_target_ids():
        return set()

    @staticmethod
    def _schedule_data_save(**_kwargs):
        return None

    async def _kick_proactive_loop_once(self):
        self.kick_count += 1


class AnonymousAreaHarness(ProactiveTransitionHarness):
    def __init__(self) -> None:
        super().__init__()
        self.area_label = "上海市·徐汇区"

    def _mobile_user_proactive_scene(self, _user, now=None):
        return {
            "available": True,
            "area_label": self.area_label,
            "presence_state": "away",
            "in_motion": False,
        }

    @staticmethod
    def _proactive_quota_policy(_user):
        return {"tier": 4, "label": "L4"}

    @staticmethod
    def _environment_fromtimestamp(timestamp):
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def test_authorized_mobile_location_is_prompt_context_not_primary_location() -> None:
    harness = MobileSceneHarness()
    snapshot = harness._build_companion_scene_snapshot({"user_id": "owner-1"})
    assert snapshot["location"]["text"] == ""
    assert snapshot["location"]["mobile"]["available"] is True
    rendered = harness._format_companion_scene_snapshot(snapshot)
    assert "手机定位上下文" in rendered
    assert "地点档案：公司（工作地点），已在标记地点范围内" in rendered
    assert "地点认知地图" in rendered
    assert "已确认地点：公司（工作地点）" in rendered
    assert "不向用户主动暴露精确坐标" in rendered


def test_authorized_mobile_location_has_dedicated_private_dialogue_context() -> None:
    harness = MobileSceneHarness()

    rendered = harness._format_mobile_user_location_context({"user_id": "owner-1"})

    assert "【用户手机位置感知】" in rendered
    assert "用户当前位于已标记地点“公司”（工作地点）范围内" in rendered
    assert "手机状态：正在使用手机，电量约 78%，正在充电" in rendered
    assert "不得把未标记地点猜成具体住址" in rendered


def test_proactive_mobile_location_is_coarse_and_does_not_expose_coordinates() -> None:
    harness = MobileSceneHarness()

    matched = harness._format_mobile_user_location_context_for_proactive({"user_id": "owner-1"})
    unmatched = harness._format_mobile_user_location_context_for_proactive({"user_id": "owner-unmatched"})

    assert "主动场景位置线索" in matched
    assert "用户当前位于已标记地点“公司”（工作地点）范围内" in matched
    assert "纬度" not in matched and "经度" not in matched
    assert "未命中已标记地点" in unmatched
    assert "31.23" not in unmatched and "121.474" not in unmatched
    assert "不要把位置本身硬写成主动话题" in unmatched


class _CountingBridgeHarness(MobileSceneHarness):
    """Count reality-bridge round trips so the formatter cannot silently regress."""

    def __init__(self) -> None:
        super().__init__()
        self.mobile_context_calls = 0
        self.map_observer_calls = 0

    def _reality_mobile_context(self, user_id: str):
        self.mobile_context_calls += 1
        return MobileSceneHarness._reality_mobile_context(user_id)

    def _observe_mobile_place_context(self, user_id, mobile_location, **kwargs):
        self.map_observer_calls += 1
        return super()._observe_mobile_place_context(user_id, mobile_location, **kwargs)


def test_proactive_location_formatter_queries_reality_bridge_once_per_pass() -> None:
    matched_harness = _CountingBridgeHarness()
    unmatched_harness = _CountingBridgeHarness()

    matched = matched_harness._format_mobile_user_location_context_for_proactive({"user_id": "owner-1"})
    unmatched = unmatched_harness._format_mobile_user_location_context_for_proactive({"user_id": "owner-unmatched"})

    assert "主动场景位置线索" in matched and "主动场景位置线索" in unmatched
    assert matched_harness.mobile_context_calls == 1
    assert matched_harness.map_observer_calls == 1
    assert unmatched_harness.mobile_context_calls == 1
    assert unmatched_harness.map_observer_calls == 1


def test_proactive_weather_timing_uses_workplace_as_context() -> None:
    harness = ProactiveLocationHarness()
    user = {"user_id": "owner-1"}

    topic = harness._choose_proactive_topic("environment_change", user)
    motive = harness._choose_proactive_motive("environment_change", user)

    assert topic == "下班前的这场雨"
    assert "下班回家前" in motive


def test_location_weather_linking_is_configurable_without_removing_the_capability() -> None:
    harness = ProactiveLocationHarness()

    assert harness._mobile_location_weather_is_safety_relevant("刚下雨") is False
    assert harness._mobile_location_weather_is_safety_relevant("暴雨") is True

    harness.config = {"mobile_location_weather_sensitivity": "sensitive"}
    assert harness._mobile_location_weather_is_safety_relevant("刚下雨") is True

    harness.config = {"mobile_location_weather_sensitivity": "quiet"}
    assert harness._mobile_location_weather_is_safety_relevant("中雨") is False
    assert harness._mobile_location_weather_is_safety_relevant("台风") is True


def test_recent_arrival_at_home_becomes_a_natural_proactive_anchor() -> None:
    harness = ProactiveTransitionHarness()
    user = {"user_id": "owner-route"}

    # The first observation establishes the previous confirmed place; the second
    # one is the actual company -> home transition that can anchor a greeting.
    harness._observe_mobile_place_context(user["user_id"], harness._reality_mobile_context(user["user_id"])["location"], observed_at=100)
    harness.current_place = ("家", "home")

    topic = harness._choose_proactive_topic("check_in", user)
    motive = harness._choose_proactive_motive("check_in", user)

    assert topic == "刚到家后的这一小段"
    assert "刚到家" in motive

    event = harness._pick_mobile_location_arrival_event(user)
    assert event is not None
    assert event["reason"] == "check_in"
    assert event["topic"] == "刚到家后的这一小段"
    assert event["mobile_location_event_type"] == "home_arrival"
    assert "last_mobile_location_arrival_key" not in user

    # Selecting/queuing an event is not delivery. A later scheduler pass must
    # still be able to offer the same transition when a send gate deferred it.
    retry = harness._pick_mobile_location_arrival_event(user)
    assert retry is not None

    user["last_mobile_location_arrival_key"] = event["_mobile_location_transition_key"]
    assert harness._pick_mobile_location_arrival_event(user) is None


def test_mobile_location_transition_is_committed_only_after_real_send() -> None:
    harness = ProactiveTransitionHarness()
    user = {"user_id": "owner-route", "planned_mobile_location_transition_key": "transition-1"}

    harness._commit_mobile_location_arrival_after_send(user)

    assert user["last_mobile_location_arrival_key"] == "transition-1"
    assert user["planned_mobile_location_transition_key"] == ""


def test_mobile_location_watch_wakes_once_for_a_new_transition() -> None:
    harness = ProactiveTransitionHarness()
    user = {"user_id": "owner-route"}
    harness._observe_mobile_place_context(user["user_id"], harness._reality_mobile_context(user["user_id"])["location"], observed_at=100)
    harness.current_place = ("家", "home")

    # The first poll establishes the baseline and deliberately does not send.
    baseline_now = time.time()
    assert asyncio.run(harness._mobile_location_watch_once(now=baseline_now)) is False
    assert harness.kick_count == 0

    harness.current_place = ("公司", "work")
    assert asyncio.run(harness._mobile_location_watch_once(now=baseline_now + 60)) is True
    assert harness.kick_count == 1
    assert asyncio.run(harness._mobile_location_watch_once(now=baseline_now + 75)) is False
    assert harness.kick_count == 1


def test_departure_from_home_becomes_a_prompt_and_proactive_anchor() -> None:
    harness = ProactiveTransitionHarness()
    harness.current_place = ("家", "home")
    user = harness.data["users"]["owner-route"]
    harness._observe_mobile_place_context(
        user["user_id"], harness._reality_mobile_context(user["user_id"])["location"], observed_at=time.time(),
    )
    harness.current_place = None

    scene = harness._mobile_user_proactive_scene(user)
    event = harness._pick_mobile_location_arrival_event(user)
    prompt = harness._format_mobile_user_location_context_for_proactive(user)

    assert scene["presence_state"] == "in_transit"
    assert scene["recent_departure"] is True
    assert scene["transition_kind"] == "departure"
    assert event is not None
    assert event["topic"] == "刚离开家后的路上"
    assert "用户刚离开已标记的家" == event["scene"]
    assert "刚离开已标记地点“家”" in prompt


def test_unconfirmed_departure_changes_presence_without_emitting_an_event() -> None:
    harness = ProactiveTransitionHarness()
    user = harness.data["users"]["owner-route"]
    context = {
        "available": True,
        "location": {
            "available": True,
            "speed_mps": 0.8,
            "place": {
                "matched": False,
                "name": "公司",
                "kind": "work",
                "confidence": "departure_confirming",
            },
        },
        "device": {
            "available": True,
            "app_state": "background",
            "battery_percent": 48,
            "charging": False,
            "stale": False,
        },
    }
    harness._reality_mobile_context = lambda _user_id: context

    scene = harness._mobile_user_proactive_scene(user)
    prompt = harness._format_mobile_user_location_context_for_proactive(user)

    assert scene["presence_state"] == "departing"
    assert scene["transition_key"] == ""
    assert scene["recent_transition"] is False
    assert harness._pick_mobile_location_arrival_event(user) is None
    assert "正在离开已标记地点“公司”" in prompt
    assert "离开事件尚未确认" in prompt


def test_low_battery_adjusts_location_event_tone_without_becoming_the_topic() -> None:
    harness = ProactiveTransitionHarness()
    harness.current_place = ("家", "home")
    user = harness.data["users"]["owner-route"]
    harness._observe_mobile_place_context(
        user["user_id"], harness._reality_mobile_context(user["user_id"])["location"], observed_at=time.time(),
    )
    harness.current_place = None
    original_context = harness._reality_mobile_context

    def low_battery_context(user_id: str):
        context = original_context(user_id)
        context["device"] = {
            "available": True,
            "app_state": "background",
            "battery_percent": 9,
            "charging": False,
            "stale": False,
        }
        return context

    harness._reality_mobile_context = low_battery_context
    event = harness._pick_mobile_location_arrival_event(user)

    assert event is not None
    assert event["tone"] == "轻一点，短一点，不邀请长通话"
    assert "电量" not in event["topic"]
    assert "电量" not in event["motive"]


def test_anonymous_area_dwell_waits_until_departure_then_delays_followup() -> None:
    harness = AnonymousAreaHarness()
    user = harness.data["users"]["owner-route"]
    started = 1_000.0

    assert harness._observe_mobile_anonymous_area(user, harness._mobile_user_proactive_scene(user), now=started) is True
    assert harness._pick_mobile_anonymous_area_event(user, now=started + 2_000) is None

    # L4 needs a stable 90-minute dwell; the candidate is created only when
    # the anonymous area is left, then scheduled with a human-like delay.
    harness._observe_mobile_anonymous_area(user, harness._mobile_user_proactive_scene(user), now=started + 5_400)
    harness.area_label = ""
    assert harness._observe_mobile_anonymous_area(user, harness._mobile_user_proactive_scene(user), now=started + 5_401) is True
    event = harness._pick_mobile_anonymous_area_event(user, now=started + 6_000)

    assert event is not None
    assert event["reason"] == "anonymous_area_dwell"
    assert event["_scheduled_ts"] > started + 6_000
    assert "上海市" not in str(event)
    assert len(user["mobile_anonymous_area_visits"][0]["token"]) == 20


def test_anonymous_area_repeat_visits_create_familiarity_without_location_name() -> None:
    harness = AnonymousAreaHarness()
    user = harness.data["users"]["owner-route"]
    base = 10_000.0

    for index in range(3):
        visit_start = base + index * 10_000
        harness.area_label = "上海市·徐汇区"
        harness._observe_mobile_anonymous_area(user, harness._mobile_user_proactive_scene(user), now=visit_start)
        harness._observe_mobile_anonymous_area(user, harness._mobile_user_proactive_scene(user), now=visit_start + 5_400)
        harness.area_label = ""
        harness._observe_mobile_anonymous_area(user, harness._mobile_user_proactive_scene(user), now=visit_start + 5_401)
        if index < 2:
            user["mobile_anonymous_area_pending"] = {}

    event = harness._pick_mobile_anonymous_area_event(user, now=base + 2 * 10_000 + 6_000)

    assert event is not None
    assert event["reason"] == "anonymous_area_familiarity"
    assert event["context"]["visit_count"] >= 3
    assert "徐汇" not in str(event)


def test_location_humanization_budget_keeps_cues_from_piling_up() -> None:
    harness = AnonymousAreaHarness()
    user = harness.data["users"]["owner-route"]
    now = 100_000.0

    assert harness._mobile_location_humanization_budget_available(user, now=now)
    user["last_mobile_location_humanization_at"] = now - 3_599
    assert not harness._mobile_location_humanization_budget_available(user, now=now)
    assert harness._mobile_location_humanization_budget_available(user, now=now + 3_600)

    harness.area_label = "上海市·徐汇区"
    user["last_mobile_location_humanization_at"] = now
    assert harness._pick_mobile_anonymous_area_event(user, now=now + 60) is None
