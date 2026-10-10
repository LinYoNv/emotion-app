from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any


CONTRACT_NAME = "chat.unified_person.v1"
CONTRACT_VERSION = "1.0"
PROJECTION_SCHEMA_VERSION = "1.0"
P3_CONTRACT_NAME = "chat.p3.context.v1"
P3_CONTRACT_VERSION = "1.0"
P3_SLOT_NAMES = ("persona", "runtime", "person", "scene")
P3_SLOT_OWNERS = {
    "persona": "companion",
    "runtime": "companion",
    "person": "memory_projection",
    "scene": "companion",
}
P3_SLOT_STATES = frozenset({"ready", "legacy_local", "invalid", "degraded", "pending"})
IDENTITY_FIELDS = (
    "companion_instance_id",
    "bot_account_id",
    "adapter_instance_id",
    "subject_namespace",
    "platform_subject_id",
)
PROJECTION_FIELDS = (
    "contract_name",
    "contract_version",
    "contract_fingerprint",
    "projection_schema_version",
    "projection_revision",
    "person_id",
    "resolved_identity_key",
    "identity_assurance",
    "profile_status",
    "display_name",
    "aliases",
    "relation_policy_id",
    "relation_label",
    "owner_mode",
    "affinity_score",
    "affinity_band",
    "relationship_capabilities",
    "group_overlay_ref",
    "updated_at",
)
IDENTITY_ASSURANCE_VALUES = frozenset({"unverified", "observed", "verified", "explicit_linked"})
PROFILE_STATUS_VALUES = frozenset({"active", "suspended", "quarantined", "deleted"})
OWNER_MODE_VALUES = frozenset({"none", "owner", "not_owner"})
AFFINITY_BAND_VALUES = frozenset({"critical", "guarded", "neutral", "positive"})
RELATIONSHIP_CAPABILITIES = frozenset(
    {
        "comfort",
        "proactive_care",
        "joking",
        "affectionate_address",
        "missing_you",
        "romantic_expression",
    }
)
CONTEXT_FORBIDDEN_KEYS = frozenset(
    {
        "raw_prompt",
        "prompt",
        "private_object",
        "private_object_ref",
        "object",
        "chat_text",
        "content",
        "messages",
        "transcript",
        "database",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, field: str, limit: int = 160) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field}_invalid")
    text = unicodedata.normalize("NFC", str(value)).strip()
    if not text or "\x00" in text or len(text) > limit:
        raise ValueError(f"{field}_invalid")
    return text


def canonical_identity(identity: dict[str, Any]) -> dict[str, str]:
    if not isinstance(identity, dict):
        raise ValueError("identity_invalid")
    result = {field: _text(identity.get(field), field) for field in IDENTITY_FIELDS}
    result["subject_namespace"] = result["subject_namespace"].lower()
    return result


def build_identity_key(identity: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical(canonical_identity(identity)).encode("utf-8")).hexdigest()
    return f"chat-origin-v1:{digest}"


def person_id_for_identity(identity: dict[str, Any]) -> str:
    return f"person_{hashlib.sha256(build_identity_key(identity).encode('utf-8')).hexdigest()[:24]}"


def affinity_band(score: Any) -> str:
    try:
        value = int(score)
    except (TypeError, ValueError):
        value = 0
    if value <= -800:
        return "critical"
    if value < 0:
        return "guarded"
    if value == 0:
        return "neutral"
    return "positive"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_person_store() -> dict[str, Any]:
    return {
        "version": 1,
        "profiles": {},
        "identity_links": {},
        "group_overlays": {},
        "relation_policies": {
            "default_friend": {
                "label": "friend",
                "capabilities": [],
                "active": True,
            }
        },
        "audit_events": [],
    }


def ensure_person_store(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not isinstance(data, dict):
        raise ValueError("store_invalid")
    changed = False
    root = data.get("unified_person")
    if not isinstance(root, dict):
        data["unified_person"] = empty_person_store()
        return data, True
    defaults = empty_person_store()
    for key, value in defaults.items():
        if key not in root or not isinstance(root.get(key), type(value)):
            root[key] = value.copy() if isinstance(value, dict) else list(value) if isinstance(value, list) else value
            changed = True
    root["version"] = max(1, int(root.get("version") or 0))
    return data, changed


def _list_text(value: Any, limit: int = 120, count: int = 12) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        try:
            text = _text(item, "list_item", limit)
        except ValueError:
            continue
        if text not in result:
            result.append(text)
        if len(result) >= count:
            break
    return result


def build_person_projection(store: dict[str, Any], person_id: str) -> dict[str, Any] | None:
    root = store.get("unified_person") if isinstance(store, dict) else None
    profiles = root.get("profiles") if isinstance(root, dict) and isinstance(root.get("profiles"), dict) else {}
    profile = profiles.get(str(person_id or ""))
    if not isinstance(profile, dict):
        return None
    identity_key = profile.get("resolved_identity_key")
    policy_id = str(profile.get("relation_policy_id") or "default_friend")
    policies = root.get("relation_policies") if isinstance(root.get("relation_policies"), dict) else {}
    policy = policies.get(policy_id) if isinstance(policies.get(policy_id), dict) else {}
    projection = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "contract_fingerprint": CONTRACT_FINGERPRINT,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "projection_revision": max(1, int(profile.get("projection_revision") or 1)),
        "person_id": str(profile.get("person_id") or ""),
        "resolved_identity_key": str(identity_key or ""),
        "identity_assurance": str(profile.get("identity_assurance") or "unverified"),
        "profile_status": str(profile.get("profile_status") or "active"),
        "display_name": str(profile.get("display_name") or ""),
        "aliases": _list_text(profile.get("aliases")),
        "relation_policy_id": policy_id,
        "relation_label": str(policy.get("label") or "friend"),
        "owner_mode": str(profile.get("owner_mode") or "not_owner"),
        "affinity_score": max(-1200, min(1200, int(profile.get("affinity_score") or 0))),
        "affinity_band": affinity_band(profile.get("affinity_score") or 0),
        "relationship_capabilities": [
            item for item in _list_text(policy.get("capabilities"), 60) if item in RELATIONSHIP_CAPABILITIES
        ],
        "group_overlay_ref": str(profile.get("group_overlay_ref") or ""),
        "updated_at": str(profile.get("updated_at") or ""),
    }
    return projection if not validate_projection(projection) else None


def validate_projection(projection: Any) -> list[str]:
    if not isinstance(projection, dict):
        return ["projection_invalid"]
    errors = [f"missing_{field}" for field in PROJECTION_FIELDS if field not in projection]
    if errors:
        return errors
    if projection.get("contract_name") != CONTRACT_NAME:
        errors.append("contract_name_mismatch")
    if projection.get("contract_version") != CONTRACT_VERSION:
        errors.append("contract_version_mismatch")
    if projection.get("contract_fingerprint") != CONTRACT_FINGERPRINT:
        errors.append("contract_fingerprint_mismatch")
    if projection.get("projection_schema_version") != PROJECTION_SCHEMA_VERSION:
        errors.append("projection_schema_version_mismatch")
    for field in ("person_id", "resolved_identity_key", "display_name", "relation_policy_id", "relation_label", "updated_at"):
        value = projection.get(field)
        if not isinstance(value, str) or not value or "\x00" in value or len(value) > 240:
            errors.append(f"{field}_invalid")
    if not re.fullmatch(r"chat-origin-v1:[0-9a-f]{64}", str(projection.get("resolved_identity_key") or "")):
        errors.append("resolved_identity_key_invalid")
    if not re.fullmatch(r"person_[0-9a-f]{24}", str(projection.get("person_id") or "")):
        errors.append("person_id_invalid")
    if not isinstance(projection.get("projection_revision"), int) or projection["projection_revision"] < 1:
        errors.append("projection_revision_invalid")
    if projection.get("identity_assurance") not in IDENTITY_ASSURANCE_VALUES:
        errors.append("identity_assurance_invalid")
    if projection.get("profile_status") not in PROFILE_STATUS_VALUES:
        errors.append("profile_status_invalid")
    if projection.get("owner_mode") not in OWNER_MODE_VALUES:
        errors.append("owner_mode_invalid")
    if not isinstance(projection.get("aliases"), list) or any(not isinstance(item, str) for item in projection["aliases"]):
        errors.append("aliases_invalid")
    if not isinstance(projection.get("relationship_capabilities"), list) or not set(projection["relationship_capabilities"]).issubset(RELATIONSHIP_CAPABILITIES):
        errors.append("relationship_capabilities_invalid")
    score = projection.get("affinity_score")
    if not isinstance(score, int) or not -1200 <= score <= 1200:
        errors.append("affinity_score_invalid")
    elif projection.get("affinity_band") != affinity_band(score):
        errors.append("affinity_band_inconsistent")
    if projection.get("group_overlay_ref") is not None and not isinstance(projection.get("group_overlay_ref"), str):
        errors.append("group_overlay_ref_invalid")
    return errors


def resolve_identity(store: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    try:
        identity_key = build_identity_key(identity)
    except ValueError:
        return {"state": "invalid", "identity_key": "", "person_id": "", "errors": ["identity_invalid"]}
    root = store.get("unified_person") if isinstance(store, dict) else None
    links = root.get("identity_links") if isinstance(root, dict) and isinstance(root.get("identity_links"), dict) else {}
    link = links.get(identity_key)
    if not isinstance(link, dict):
        return {"state": "pending", "identity_key": identity_key, "person_id": ""}
    person_id = str(link.get("person_id") or "")
    projection = build_person_projection(store, person_id)
    if projection is None:
        return {"state": "invalid", "identity_key": identity_key, "person_id": person_id, "errors": ["projection_invalid"]}
    return {"state": "resolved", "identity_key": identity_key, "person_id": person_id, "projection": projection}


def _safe_context_value(value: Any, depth: int = 0) -> Any:
    if depth > 2:
        return None
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (list, tuple)):
        return [_safe_context_value(item, depth + 1) for item in list(value)[:12]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:24]:
            name = str(key).strip().lower()
            if not name or name in CONTEXT_FORBIDDEN_KEYS:
                continue
            safe = _safe_context_value(item, depth + 1)
            if safe is not None:
                result[name[:80]] = safe
        return result
    return None


def make_context_slot(slot: str, payload: dict[str, Any] | None, *, owner: str = "", revision: int = 1, state: str = "ready", warnings: list[str] | None = None) -> dict[str, Any]:
    slot_name = str(slot or "")
    actual_owner = owner or P3_SLOT_OWNERS.get(slot_name, "unknown")
    return {
        "slot": slot_name,
        "owner": actual_owner,
        "revision": max(1, int(revision or 1)),
        "contract_name": P3_CONTRACT_NAME,
        "contract_version": P3_CONTRACT_VERSION,
        "contract_fingerprint": P3_CONTRACT_FINGERPRINT,
        "state": state if state in P3_SLOT_STATES else "invalid",
        "payload": _safe_context_value(payload or {}) or {},
        "warnings": [str(item)[:120] for item in (warnings or [])[:8]],
    }


def validate_context_slot(slot: Any, expected_name: str = "") -> list[str]:
    if not isinstance(slot, dict):
        return ["slot_invalid"]
    errors: list[str] = []
    name = str(slot.get("slot") or "")
    if name not in P3_SLOT_NAMES or (expected_name and name != expected_name):
        errors.append("slot_name_invalid")
    if slot.get("owner") != P3_SLOT_OWNERS.get(name):
        errors.append("slot_owner_mismatch")
    if slot.get("contract_name") != P3_CONTRACT_NAME or slot.get("contract_version") != P3_CONTRACT_VERSION:
        errors.append("slot_contract_mismatch")
    if slot.get("contract_fingerprint") != P3_CONTRACT_FINGERPRINT:
        errors.append("slot_fingerprint_mismatch")
    if not isinstance(slot.get("revision"), int) or slot["revision"] < 1:
        errors.append("slot_revision_invalid")
    if slot.get("state") not in P3_SLOT_STATES:
        errors.append("slot_state_invalid")
    if not isinstance(slot.get("payload"), dict):
        errors.append("slot_payload_invalid")
    return errors


def build_context_projection(slots: dict[str, dict[str, Any]] | None = None, *, state: str = "ready", revision: int = 1, warnings: list[str] | None = None) -> dict[str, Any]:
    incoming = slots if isinstance(slots, dict) else {}
    return {
        "contract_name": P3_CONTRACT_NAME,
        "contract_version": P3_CONTRACT_VERSION,
        "contract_fingerprint": P3_CONTRACT_FINGERPRINT,
        "revision": max(1, int(revision or 1)),
        "state": state if state in P3_SLOT_STATES else "invalid",
        "slots": {name: incoming.get(name, make_context_slot(name, {}, state="pending")) for name in P3_SLOT_NAMES},
        "warnings": [str(item)[:120] for item in (warnings or [])[:8]],
    }


def validate_context_projection(context: Any) -> list[str]:
    if not isinstance(context, dict):
        return ["context_invalid"]
    errors: list[str] = []
    if context.get("contract_name") != P3_CONTRACT_NAME or context.get("contract_version") != P3_CONTRACT_VERSION:
        errors.append("context_contract_mismatch")
    if context.get("contract_fingerprint") != P3_CONTRACT_FINGERPRINT:
        errors.append("context_fingerprint_mismatch")
    slots = context.get("slots")
    if not isinstance(slots, dict):
        return errors + ["context_slots_invalid"]
    for name in P3_SLOT_NAMES:
        errors.extend(validate_context_slot(slots.get(name), name))
    return errors


def merge_context_slots(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing) if isinstance(existing, dict) else build_context_projection()
    old_slots = result.get("slots") if isinstance(result.get("slots"), dict) else {}
    new_slots = incoming.get("slots") if isinstance(incoming, dict) and isinstance(incoming.get("slots"), dict) else {}
    merged = dict(old_slots)
    for name in P3_SLOT_NAMES:
        candidate = new_slots.get(name)
        if not isinstance(candidate, dict) or validate_context_slot(candidate, name):
            continue
        previous = merged.get(name)
        if not isinstance(previous, dict) or int(candidate.get("revision") or 0) >= int(previous.get("revision") or 0):
            merged[name] = candidate
    result["slots"] = merged
    result["revision"] = max(int(result.get("revision") or 1), int(incoming.get("revision") or 1) if isinstance(incoming, dict) else 1)
    return result


def _contract_shape() -> dict[str, Any]:
    return {
        "name": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "identity_fields": list(IDENTITY_FIELDS),
        "projection_fields": list(PROJECTION_FIELDS),
        "p3_name": P3_CONTRACT_NAME,
        "p3_version": P3_CONTRACT_VERSION,
        "p3_slots": list(P3_SLOT_NAMES),
        "p3_owners": dict(P3_SLOT_OWNERS),
    }


CONTRACT_FINGERPRINT = hashlib.sha256(_canonical(_contract_shape()).encode("utf-8")).hexdigest()[:16]
P3_CONTRACT_FINGERPRINT = hashlib.sha256(
    _canonical({"name": P3_CONTRACT_NAME, "version": P3_CONTRACT_VERSION, "slots": list(P3_SLOT_NAMES), "owners": P3_SLOT_OWNERS}).encode("utf-8")
).hexdigest()[:16]


def contract_self_check() -> list[str]:
    errors: list[str] = []
    if CONTRACT_FINGERPRINT != hashlib.sha256(_canonical(_contract_shape()).encode("utf-8")).hexdigest()[:16]:
        errors.append("contract_fingerprint_stale")
    if P3_CONTRACT_FINGERPRINT != hashlib.sha256(
        _canonical({"name": P3_CONTRACT_NAME, "version": P3_CONTRACT_VERSION, "slots": list(P3_SLOT_NAMES), "owners": P3_SLOT_OWNERS}).encode("utf-8")
    ).hexdigest()[:16]:
        errors.append("p3_contract_fingerprint_stale")
    if set(P3_SLOT_NAMES) != set(P3_SLOT_OWNERS):
        errors.append("p3_slot_owner_mismatch")
    return errors


__all__ = [name for name in globals() if name.isupper() or name in {
    "affinity_band", "build_context_projection", "build_identity_key", "build_person_projection",
    "canonical_identity", "contract_self_check", "empty_person_store", "ensure_person_store",
    "make_context_slot", "merge_context_slots", "person_id_for_identity", "resolve_identity",
    "validate_context_projection", "validate_context_slot", "validate_projection",
}]
