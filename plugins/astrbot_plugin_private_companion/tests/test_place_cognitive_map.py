# -*- coding: utf-8 -*-
from __future__ import annotations

from astrbot_plugin_private_companion.place_cognitive_map import PlaceCognitiveMapMixin


class _MapHarness(PlaceCognitiveMapMixin):
    def __init__(self) -> None:
        self.data: dict = {}
        self.saved = 0
        self.events: list[dict] = []

    def _schedule_data_save(self, **_kwargs) -> None:
        self.saved += 1

    def _place_cognitive_map_emit_memory_event(self, event: dict) -> None:
        self.events.append(event)


def _location(
    name: str,
    kind: str = "custom",
    *,
    matched: bool = True,
    confidence: str = "",
    aliases: list[str] | None = None,
    parent_name: str = "",
) -> dict:
    return {
        "available": True,
        "place": {
            "matched": matched,
            "name": name,
            "kind": kind,
            "radius_m": 120,
            "confidence": confidence,
            "aliases": aliases or [],
            "parent_name": parent_name,
        },
    }


def test_arrival_is_deduplicated_while_the_user_remains_at_a_confirmed_place() -> None:
    harness = _MapHarness()

    first = harness._observe_mobile_place_context("u1", _location("公司", "work"), observed_at=100)
    second = harness._observe_mobile_place_context("u1", _location("公司", "work"), observed_at=120)

    assert first["current_place"]["name"] == "公司"
    assert second["known_places"] == [{"name": "公司", "kind": "work"}]
    assert [item["kind"] for item in harness.events] == ["confirmed_place_arrival"]
    assert harness.saved == 1


def test_leaving_and_arriving_elsewhere_creates_a_semantic_route_and_memory_events() -> None:
    harness = _MapHarness()

    harness._observe_mobile_place_context("u1", _location("家", "home"), observed_at=100)
    result = harness._observe_mobile_place_context("u1", _location("公司", "work"), observed_at=200)

    assert result["current_place"] == {"name": "公司", "kind": "work"}
    assert result["recent_routes"] == [{"from_name": "家", "to_name": "公司", "count": 1}]
    assert [item["kind"] for item in harness.events] == [
        "confirmed_place_arrival",
        "confirmed_place_departure",
        "confirmed_place_arrival",
    ]
    assert "从家前往" in harness.events[-1]["title"]


def test_unmatched_travel_keeps_the_departure_origin_for_the_next_arrival() -> None:
    harness = _MapHarness()

    harness._observe_mobile_place_context("u1", _location("公司", "work"), observed_at=100)
    departure = harness._observe_mobile_place_context(
        "u1", _location("途中", matched=False), observed_at=160, include_transition=True,
    )
    arrival = harness._observe_mobile_place_context(
        "u1", _location("家", "home"), observed_at=260, include_transition=True,
    )

    assert departure["last_transition"]["kind"] == "departure"
    assert departure["last_transition"]["from_name"] == "公司"
    assert arrival["recent_routes"] == [{"from_name": "公司", "to_name": "家", "count": 1}]
    assert arrival["last_transition"] == {
        "kind": "arrival",
        "from_name": "公司",
        "from_kind": "work",
        "to_name": "家",
        "to_kind": "home",
        "at": "1970-01-01T00:04:20+00:00",
    }
    assert [item["kind"] for item in harness.events] == [
        "confirmed_place_arrival",
        "confirmed_place_departure",
        "confirmed_place_arrival",
    ]
    assert "从公司前往" in harness.events[-1]["title"]


def test_unmatched_place_does_not_create_any_durable_place_knowledge() -> None:
    harness = _MapHarness()

    result = harness._observe_mobile_place_context("u1", _location("临时猜测", matched=False), observed_at=100)

    assert result["available"] is False
    assert "place_cognitive_maps" not in harness.data or harness.data["place_cognitive_maps"] == {}
    assert harness.events == []


def test_uncertain_boundary_does_not_create_arrival_memory() -> None:
    harness = _MapHarness()

    result = harness._observe_mobile_place_context(
        "u1",
        _location("公司", "work", confidence="boundary_uncertain"),
        observed_at=100,
    )

    assert result["available"] is False
    assert harness.events == []


def test_place_aliases_and_parent_enter_semantic_map_without_coordinates() -> None:
    harness = _MapHarness()

    result = harness._observe_mobile_place_context(
        "u1",
        _location("办公室", "work", confidence="confirmed", aliases=["单位"], parent_name="科技园"),
        observed_at=100,
    )

    rendered = harness._format_place_cognitive_map_context(result)
    assert "办公室（工作地点，也叫单位，位于科技园）" in rendered
    assert "latitude" not in repr(harness.data)
    assert "longitude" not in repr(harness.data)
