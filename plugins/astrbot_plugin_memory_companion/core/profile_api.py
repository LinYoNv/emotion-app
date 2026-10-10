"""Read-only, privacy-limited C4 Bot Profile projection.

This module deliberately operates on already-loaded records and never exposes
record content, evidence, or payload data.  It is suitable as a small boundary
for later service/bridge integration.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any


PROFILE_NAMES = (
    "bot_schedule_current",
    "bot_schedule_history",
    "bot_creative",
    "bot_subjective",
    "locked_frame_personal",
)

_PROFILE_TYPES = {
    "bot_schedule_current": frozenset(
        {
            "bot_schedule_plan",
            "bot_observed_activity",
            "bot_schedule_reconciliation",
            "bot_window_snapshot",
            "bot_shared_activity",
            "bot_detail_fragment",
            "bot_calendar_event",
            "bot_proactive_message",
        }
    ),
    "bot_schedule_history": frozenset(
        {
            "bot_schedule_plan",
            "bot_observed_activity",
            "bot_schedule_reconciliation",
            "bot_window_snapshot",
            "bot_daily_diary",
            "bot_shared_activity",
            "bot_detail_fragment",
            "bot_calendar_event",
            "bot_proactive_message",
        }
    ),
    "bot_creative": frozenset({"bot_creative_work", "bot_media_memory"}),
    "bot_subjective": frozenset({"bot_daily_diary", "bot_subjective_memory"}),
    "locked_frame_personal": frozenset(
        {
            "bot_schedule_plan",
            "bot_observed_activity",
            "bot_schedule_reconciliation",
            "bot_window_snapshot",
            "bot_daily_diary",
            "bot_creative_work",
            "bot_media_memory",
            "bot_subjective_memory",
            "bot_shared_activity",
            "bot_detail_fragment",
            "bot_calendar_event",
            "bot_proactive_message",
        }
    ),
}

# Public lookup is intentionally keyed by the C3 memory type.  A type may be
# visible in more than one profile, while the private profile index above
# keeps selection logic straightforward.
PROFILE_MEMORY_TYPES = {
    memory_type: frozenset(
        profile for profile, memory_types in _PROFILE_TYPES.items() if memory_type in memory_types
    )
    for memory_type in sorted({item for values in _PROFILE_TYPES.values() for item in values})
}

_BOT_DOMAIN = "bot_self_schedule"
_FORBIDDEN_SCOPE_OR_VISIBILITY = {"group", "user_memory"}
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{0,240}$")
_SENSITIVE = re.compile(
    r"(?i)(?:bearer\s+|password\s*[:=]|passwd\s*[:=]|secret\s*[:=]|"
    r"token\s*[:=]|api[_-]?key\s*[:=]|authorization\s*[:=]|base64,|"
    r"(?:[a-z]:[\\/]|/home/|/root/|/tmp/|/var/))"
)

_SUMMARY_BY_TYPE = {
    "bot_schedule_plan": "Bot schedule plan record.",
    "bot_observed_activity": "Bot observed activity record.",
    "bot_schedule_reconciliation": "Bot schedule reconciliation record.",
    "bot_window_snapshot": "Bot window snapshot record.",
    "bot_daily_diary": "Bot daily diary record.",
    "bot_creative_work": "Bot creative work record.",
    "bot_media_memory": "Bot media memory record.",
    "bot_subjective_memory": "Bot subjective memory record.",
    "bot_shared_activity": "Bot shared activity record.",
    "bot_detail_fragment": "Bot schedule detail record.",
    "bot_calendar_event": "Bot calendar event record.",
    "bot_proactive_message": "Bot proactive message record.",
}


def profile_names() -> tuple[str, ...]:
    """Return the stable profile names without exposing mutable state."""

    return PROFILE_NAMES


def profile_descriptor(profile: Any) -> dict[str, Any]:
    """Return a small, non-record descriptor for a profile name."""

    name = _text(profile, 80)
    return {
        "profile": name,
        "known": name in PROFILE_NAMES,
        "memory_types": sorted(_PROFILE_TYPES.get(name, ())),
        "read_only": True,
    }


def _text(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    try:
        text = str(value).strip().replace("\u3000", " ")
    except Exception:
        return ""
    if not text or not _SAFE_TEXT.fullmatch(text[:limit]) or _SENSITIVE.search(text):
        return ""
    return text[:limit]


def _metadata(record: Any) -> Mapping[str, Any]:
    value = getattr(record, "metadata", {})
    return value if isinstance(value, Mapping) else {}


def _record_value(record: Any, metadata: Mapping[str, Any], key: str, default: Any = "") -> Any:
    value = metadata.get(key, default)
    if value in (None, ""):
        value = getattr(record, key, default)
    return value


def _is_allowed_record(record: Any, metadata: Mapping[str, Any]) -> bool:
    domain = _text(metadata.get("memory_domain"), 80)
    bot_personal = metadata.get("bot_personal") is True
    scope = _text(getattr(record, "scope", ""), 40).lower()
    visibility = _text(getattr(record, "visibility", ""), 40).lower()
    metadata_scope = _text(metadata.get("scope"), 40).lower()
    metadata_visibility = _text(metadata.get("visibility"), 40).lower()
    if domain and domain != _BOT_DOMAIN:
        return False
    if not domain and not bot_personal:
        return False
    if scope in _FORBIDDEN_SCOPE_OR_VISIBILITY or visibility in _FORBIDDEN_SCOPE_OR_VISIBILITY:
        return False
    if metadata_scope in _FORBIDDEN_SCOPE_OR_VISIBILITY or metadata_visibility in _FORBIDDEN_SCOPE_OR_VISIBILITY:
        return False
    if domain == "user_memory" or metadata_visibility == "user_memory":
        return False
    return True


def _source_refs(value: Any) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)):
        values = [value]
    elif isinstance(value, Iterable):
        try:
            values = list(value)
        except Exception:
            values = []
    else:
        values = []
    result: list[str] = []
    for item in values[:16]:
        safe = _text(item, 240)
        if safe:
            result.append(safe)
    return result


def _version(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 1
    return max(1, min(2**31 - 1, number))


def _item(record: Any, metadata: Mapping[str, Any], memory_type: str) -> dict[str, Any]:
    record_id = _text(_record_value(record, metadata, "record_id", getattr(record, "id", "")), 160)
    # A bot_personal marker is an admission signal, not permission to echo an
    # arbitrary caller-supplied domain into the public projection.
    domain = _BOT_DOMAIN
    date = _text(_record_value(record, metadata, "date"), 20)
    window = _text(_record_value(record, metadata, "window"), 40)
    occurred_at = _text(_record_value(record, metadata, "occurred_at"), 80)
    source_kind = _text(_record_value(record, metadata, "source_kind"), 80)
    evidence_level = _text(_record_value(record, metadata, "evidence_level"), 40)
    status = _text(_record_value(record, metadata, "status"), 80)
    return {
        "record_id": record_id,
        "memory_domain": domain,
        "memory_type": memory_type,
        "subject": "bot_self",
        "date": date,
        "window": window,
        "occurred_at": occurred_at,
        "source_kind": source_kind,
        "source_refs": _source_refs(_record_value(record, metadata, "source_refs")),
        "evidence_level": evidence_level,
        "status": status,
        "version": _version(_record_value(record, metadata, "version", 1)),
        "summary": _SUMMARY_BY_TYPE.get(memory_type, "Bot personal memory record."),
        "reference": f"profile:{record_id}" if record_id else "profile:unknown",
    }


def _matches_query(item: Mapping[str, Any], query: str) -> bool:
    needle = _text(query, 240).casefold()
    if not needle:
        return True
    safe_fields = (
        "record_id", "memory_domain", "memory_type", "subject", "date", "window",
        "occurred_at", "source_kind", "evidence_level", "status", "version", "summary",
        "reference",
    )
    haystack = " ".join(str(item.get(key, "")) for key in safe_fields)
    haystack += " " + " ".join(item.get("source_refs", []))
    return needle in haystack.casefold()


def _limit(value: Any) -> int:
    try:
        return max(1, min(100, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 10


def build_profile_result(
    records: Iterable[Any],
    profile: Any,
    query: str = "",
    limit: int = 10,
    *,
    current_date: str = "",
    current_window: str = "",
    authorized: bool = False,
) -> dict[str, Any]:
    """Build a fixed-shape, read-only profile result without raw memory data."""

    name = _text(profile, 80)
    result: dict[str, Any] = {
        "ok": True,
        "read_only": True,
        "state": "ready",
        "degraded": False,
        "pending": False,
        "profile": name,
        "items": [],
        "warnings": [],
    }
    if name not in PROFILE_NAMES:
        result.update(ok=False, state="invalid", warnings=["unknown_profile"])
        return result
    if name == "locked_frame_personal" and authorized is not True:
        result.update(ok=False, state="forbidden", warnings=["authorization_required"])
        return result

    safe_date = _text(current_date, 20)
    safe_window = _text(current_window, 40)
    raw_query = str(query or "").strip()
    safe_query = _text(query, 240)
    if current_date and not safe_date:
        result["warnings"].append("invalid_current_date")
    if current_window and not safe_window:
        result["warnings"].append("invalid_current_window")
    if raw_query and not safe_query:
        result["warnings"].append("unsafe_query_rejected")
        return result

    try:
        iterable = () if records is None or isinstance(records, (str, bytes, bytearray)) else records
        iterator = iter(iterable)
    except Exception:
        result.update(state="degraded", degraded=True, warnings=[*result["warnings"], "invalid_records"])
        return result

    selected: list[dict[str, Any]] = []
    type_filter = _PROFILE_TYPES[name]
    try:
        for record in iterator:
            try:
                metadata = _metadata(record)
                memory_type = _text(getattr(record, "memory_type", metadata.get("memory_type", "")), 80)
                if not _is_allowed_record(record, metadata) or memory_type not in type_filter:
                    continue
                date = _text(_record_value(record, metadata, "date"), 20)
                window = _text(_record_value(record, metadata, "window"), 40)
                if name == "bot_schedule_current":
                    if safe_date and date != safe_date:
                        continue
                    if safe_window and window != safe_window:
                        continue
                elif name == "bot_schedule_history":
                    if safe_date and safe_window and date == safe_date and window == safe_window:
                        continue
                item = _item(record, metadata, memory_type)
                if _matches_query(item, safe_query):
                    selected.append(item)
            except Exception:
                result["warnings"].append("malformed_record_skipped")
    except Exception:
        result.update(state="degraded", degraded=True)
        result["warnings"].append("records_iteration_failed")

    selected.sort(key=lambda item: (item.get("occurred_at", ""), item.get("record_id", "")), reverse=True)
    result["items"] = selected[: _limit(limit)]
    if len(result["warnings"]) > 8:
        result["warnings"] = result["warnings"][:8]
    return result


__all__ = [
    "PROFILE_NAMES",
    "PROFILE_MEMORY_TYPES",
    "build_profile_result",
    "profile_descriptor",
    "profile_names",
]
