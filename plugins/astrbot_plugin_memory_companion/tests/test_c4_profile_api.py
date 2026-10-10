from dataclasses import dataclass, field

from core.profile_api import PROFILE_MEMORY_TYPES, PROFILE_NAMES, build_profile_result


@dataclass
class Record:
    id: str
    memory_type: str
    metadata: dict = field(default_factory=dict)
    scope: str = "private"
    visibility: str = "bot_self"
    content: str = "RAW CONTENT MUST NOT ESCAPE"
    evidence: str = "RAW EVIDENCE MUST NOT ESCAPE"
    occurred_at: str = ""


def rec(record_id, memory_type, *, date="2026-07-30", window="afternoon", domain="bot_self_schedule", **metadata):
    data = {
        "memory_domain": domain,
        "date": date,
        "window": window,
        "occurred_at": f"{date}T15:00:00+08:00",
        "source_kind": "observed",
        "source_refs": [f"safe:{record_id}"],
        "evidence_level": "L2",
        "status": "active",
        "version": 1,
    }
    data.update(metadata)
    return Record(record_id, memory_type, data)


def test_contract_exports_and_five_profiles_have_fixed_shape():
    assert PROFILE_NAMES == (
        "bot_schedule_current", "bot_schedule_history", "bot_creative",
        "bot_subjective", "locked_frame_personal",
    )
    assert {
        "bot_schedule_plan", "bot_observed_activity", "bot_schedule_reconciliation",
        "bot_window_snapshot", "bot_daily_diary", "bot_creative_work", "bot_media_memory",
        "bot_subjective_memory", "bot_shared_activity", "bot_detail_fragment",
        "bot_calendar_event", "bot_proactive_message",
    }.issubset(PROFILE_MEMORY_TYPES)
    assert set(PROFILE_MEMORY_TYPES["bot_creative_work"]) == {"bot_creative", "locked_frame_personal"}
    records = [
        rec("plan", "bot_schedule_plan"), rec("creative", "bot_creative_work"),
        rec("subjective", "bot_subjective_memory"),
    ]
    for profile in PROFILE_NAMES[:4]:
        result = build_profile_result(records, profile, current_date="2026-07-30", current_window="afternoon")
        assert set(result) == {"ok", "read_only", "state", "degraded", "pending", "profile", "items", "warnings"}
        assert result["read_only"] is True


def test_bot_user_group_isolation_and_bot_personal_flag():
    records = [
        rec("bot", "bot_observed_activity"),
        rec("user", "bot_observed_activity", domain="user_memory"),
        rec("group", "bot_observed_activity", domain="bot_self_schedule", scope="group", visibility="group"),
        Record("flag", "bot_observed_activity", {"bot_personal": True, "date": "2026-07-30", "window": "afternoon"}),
    ]
    result = build_profile_result(records, "bot_schedule_current", current_date="2026-07-30", current_window="afternoon")
    assert {item["record_id"] for item in result["items"]} == {"bot", "flag"}


def test_current_and_history_partition_by_date_and_window():
    records = [
        rec("current", "bot_schedule_plan", date="2026-07-30", window="afternoon"),
        rec("old-window", "bot_schedule_plan", date="2026-07-30", window="morning"),
        rec("old-date", "bot_schedule_plan", date="2026-07-29", window="afternoon"),
    ]
    current = build_profile_result(records, "bot_schedule_current", current_date="2026-07-30", current_window="afternoon")
    history = build_profile_result(records, "bot_schedule_history", current_date="2026-07-30", current_window="afternoon")
    assert [item["record_id"] for item in current["items"]] == ["current"]
    assert {item["record_id"] for item in history["items"]} == {"old-window", "old-date"}


def test_locked_profile_requires_authorization_and_returns_all_bot_types_when_allowed():
    records = [rec("creative", "bot_creative_work"), rec("subjective", "bot_subjective_memory")]
    denied = build_profile_result(records, "locked_frame_personal", authorized=False)
    allowed = build_profile_result(records, "locked_frame_personal", authorized=True)
    assert denied["ok"] is False and denied["state"] == "forbidden" and denied["items"] == []
    assert {item["record_id"] for item in allowed["items"]} == {"creative", "subjective"}


def test_no_payload_content_or_evidence_leak_and_query_only_safe_fields():
    record = rec("safe", "bot_creative_work", summary="SECRET PAYLOAD SHOULD NOT BE USED")
    result = build_profile_result([record], "bot_creative", query="creative work")
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert set(item) == {
        "record_id", "memory_domain", "memory_type", "subject", "date", "window", "occurred_at",
        "source_kind", "source_refs", "evidence_level", "status", "version", "summary", "reference",
    }
    assert "RAW CONTENT" not in str(result)
    assert "RAW EVIDENCE" not in str(result)
    assert "SECRET PAYLOAD" not in str(result)
    assert build_profile_result([record], "bot_creative", query="RAW CONTENT")["items"] == []


def test_unknown_profile_limit_and_malformed_input_degrade_safely():
    invalid = build_profile_result([], "unknown")
    assert invalid["ok"] is False and invalid["state"] == "invalid"
    assert len(build_profile_result([rec(str(i), "bot_creative_work") for i in range(120)], "bot_creative", limit=100)["items"]) == 100
    assert len(build_profile_result([rec("one", "bot_creative_work")], "bot_creative", limit="bad")["items"]) == 1
    malformed = build_profile_result(None, "bot_creative")
    assert malformed["state"] == "ready" and malformed["items"] == []
    malformed_records = build_profile_result(42, "bot_creative")
    assert malformed_records["state"] == "degraded" and malformed_records["ok"] is True
