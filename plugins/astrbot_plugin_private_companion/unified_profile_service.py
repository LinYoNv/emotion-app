"""Companion-owned REQ-036 capability state and local migration helpers."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .unified_profile_contract import build_capability_summary, normalize_portrait_mode


CAPABILITY_SCHEMA_VERSION = 1
MIGRATION_KEY = "req036_capability_migration"
DEFAULT_CLOSED_REPAIR_OPERATION_ID = "req036-default-closed-compat-v3"
DEFAULT_UNAUTHORIZED_PRIVATE_REPLY = "老大不让我跟陌生人说话哦。"
_MIGRATION_SNAPSHOT_FIELDS = (
    "unified_profile_capabilities",
    "private_companion_enabled",
    "proactive_private_enabled",
    "enabled",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, limit: int = 120) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    result = str(value).strip()
    return result[:limit] if result and "\x00" not in result else ""


def _bool(value: Any) -> bool:
    return value is True


def _legacy_private_enabled(user: dict[str, Any]) -> bool:
    # Read the historical effective state only to avoid accidentally granting
    # proactive permission during migration. It no longer gates passive chat.
    if _bool(user.get("manual_disabled")):
        return False
    if "private_companion_enabled" in user:
        return _bool(user.get("private_companion_enabled"))
    return _bool(user.get("enabled"))


def _legacy_proactive_enabled(user: dict[str, Any], private_enabled: bool) -> bool:
    if "proactive_private_enabled" in user:
        return _bool(user.get("proactive_private_enabled"))
    # The old schema had no capability field.  A non-zero per-user daily
    # budget was the durable configuration signal; cooldowns and schedules are
    # deliberately excluded from this migration.
    try:
        return private_enabled and int(user.get("proactive_daily_limit") or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _legacy_positive_daily_limit(user: dict[str, Any]) -> bool:
    try:
        return int(user.get("proactive_daily_limit") or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _latest_explicit_capability_decision(user: dict[str, Any], capability: str) -> bool | None:
    audit = user.get("unified_profile_capability_audit")
    if not isinstance(audit, list):
        return None
    for entry in reversed(audit):
        if not isinstance(entry, dict):
            continue
        changed = entry.get("changed")
        change = changed.get(capability) if isinstance(changed, dict) else None
        if not isinstance(change, dict) or type(change.get("to")) is not bool:
            continue
        actor = _text(entry.get("actor_id"), 120).lower()
        reason = _text(entry.get("reason_code"), 80).lower()
        if actor in {"compatibility_migration", "startup_migration"} or any(
            token in reason for token in ("legacy_", "migration", "compatibility", "reconciliation")
        ):
            continue
        return change["to"]
    return None


def _default_closed_repair_plan(
    user: dict[str, Any],
    *,
    legacy_snapshot: Any = None,
) -> dict[str, Any] | None:
    """Recover only legacy permission evidence overwritten by default-closed migration."""
    existing = user.get("unified_profile_capabilities")
    if not isinstance(existing, dict):
        return None
    try:
        if int(existing.get("schema_version") or 0) != CAPABILITY_SCHEMA_VERSION:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    grant_source = _text(existing.get("grant_source"), 80).lower()
    if grant_source not in {"", "default_closed"}:
        return None
    if (
        bool(user.get("manual_disabled"))
        or bool(user.get("auto_profile_created"))
        or _text(user.get("profile_origin"), 80).lower() == "private_auto"
    ):
        return None

    private_decision = _latest_explicit_capability_decision(user, "private_companion_enabled")
    proactive_decision = _latest_explicit_capability_decision(user, "proactive_private_enabled")
    private_enabled = True
    proactive_enabled = _bool(existing.get("proactive_private_enabled"))
    snapshot_private_enabled = False
    if isinstance(legacy_snapshot, dict):
        snapshot_private_enabled = _bool(legacy_snapshot.get("private_companion_enabled"))
        if not snapshot_private_enabled:
            present_fields = legacy_snapshot.get("_present_fields")
            enabled_was_present = (
                "enabled" in present_fields
                if isinstance(present_fields, list)
                else "enabled" in legacy_snapshot
            )
            snapshot_private_enabled = enabled_was_present and _bool(legacy_snapshot.get("enabled"))
    private_evidence = private_decision is True or any(
        _bool(user.get(key))
        for key in (
            "private_companion_enabled",
            "proactive_private_enabled",
            "manual_enabled",
            "auto_enabled",
        )
    ) or snapshot_private_enabled
    legacy_private_allowed = private_decision is not False and private_evidence
    proactive_evidence = (
        proactive_decision is True
        or _bool(user.get("proactive_private_enabled"))
        or _legacy_positive_daily_limit(user)
    )
    repaired_proactive = proactive_enabled or (
        legacy_private_allowed
        and proactive_decision is not False
        and proactive_evidence
    )
    if repaired_proactive == proactive_enabled:
        return None
    return {
        "private_companion_enabled": True,
        "proactive_private_enabled": repaired_proactive,
        "grant_source": "legacy_default_closed_repair",
    }


def _migration_snapshot_presence(snapshot: Any) -> tuple[bool, set[str] | None]:
    if not isinstance(snapshot, dict):
        return False, None
    allowed_snapshot_keys = set(_MIGRATION_SNAPSHOT_FIELDS) | {"_present_fields"}
    if any(type(key) is not str or key not in allowed_snapshot_keys for key in snapshot):
        return False, None
    if any(key not in snapshot for key in _MIGRATION_SNAPSHOT_FIELDS):
        return False, None
    present_fields = snapshot.get("_present_fields")
    if present_fields is None:
        return True, None
    if (
        not isinstance(present_fields, list)
        or any(type(key) is not str or key not in _MIGRATION_SNAPSHOT_FIELDS for key in present_fields)
        or len(present_fields) != len(set(present_fields))
    ):
        return False, None
    return True, set(present_fields)


def _migration_snapshots_valid(snapshots: Any) -> bool:
    if not isinstance(snapshots, dict):
        return False
    return all(
        isinstance(user_id, str)
        and bool(user_id)
        and _migration_snapshot_presence(snapshot)[0]
        for user_id, snapshot in snapshots.items()
    )


def default_capabilities(*, grant_source: str = "default_closed") -> dict[str, Any]:
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "private_companion_enabled": True,
        "proactive_private_enabled": False,
        "portrait_mode": "disabled",
        "portrait_mode_override": "follow_global",
        "grant_source": _text(grant_source, 80) or "default_closed",
        "updated_at": _now(),
    }


def normalize_capabilities(value: Any, *, default_source: str = "default_closed") -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = default_capabilities(grant_source=_text(source.get("grant_source"), 80) or default_source)
    result.update(
        {
            "private_companion_enabled": True,
            "proactive_private_enabled": _bool(source.get("proactive_private_enabled")),
            "portrait_mode": normalize_portrait_mode(source.get("portrait_mode")),
            "portrait_mode_override": "explicit" if _text(source.get("portrait_mode_override"), 40) == "explicit" else "follow_global",
            "updated_at": _text(source.get("updated_at"), 80) or result["updated_at"],
        }
    )
    return result


def ensure_new_profile_capabilities(
    user: dict[str, Any],
    *,
    private_companion_enabled: bool = True,
    proactive_private_enabled: bool = False,
    grant_source: str = "default_closed",
) -> dict[str, Any]:
    """Install explicit defaults for a newly observed profile only."""
    if not isinstance(user, dict):
        return default_capabilities(grant_source=grant_source)
    existing = user.get("unified_profile_capabilities")
    if isinstance(existing, dict):
        normalized = normalize_capabilities(existing)
    else:
        normalized = default_capabilities(grant_source=grant_source)
    normalized["private_companion_enabled"] = True
    normalized["proactive_private_enabled"] = bool(proactive_private_enabled)
    normalized["grant_source"] = _text(grant_source, 80) or "default_closed"
    normalized["updated_at"] = _now()
    user["unified_profile_capabilities"] = normalized
    # These legacy aliases remain readable by older code, but cannot reopen a
    # REQ-036 profile after it has been created.
    user["private_companion_enabled"] = normalized["private_companion_enabled"]
    user["proactive_private_enabled"] = normalized["proactive_private_enabled"]
    return normalized


def ensure_legacy_profile_capabilities(user: dict[str, Any]) -> dict[str, Any]:
    """Materialize durable legacy permission evidence for an existing profile."""
    if not isinstance(user, dict):
        return default_capabilities(grant_source="legacy_effective_migration")
    existing = user.get("unified_profile_capabilities")
    if isinstance(existing, dict):
        normalized = normalize_capabilities(existing)
        repair = _default_closed_repair_plan(user)
        if repair is not None:
            normalized["private_companion_enabled"] = repair["private_companion_enabled"]
            normalized["proactive_private_enabled"] = repair["proactive_private_enabled"]
            normalized["grant_source"] = repair["grant_source"]
            normalized["updated_at"] = _now()
    else:
        private_decision = _latest_explicit_capability_decision(user, "private_companion_enabled")
        proactive_decision = _latest_explicit_capability_decision(user, "proactive_private_enabled")
        legacy_private_enabled = _legacy_private_enabled(user)
        private_enabled = True
        if proactive_decision is not None:
            proactive_enabled = proactive_decision
        else:
            proactive_enabled = _legacy_proactive_enabled(user, legacy_private_enabled)
            if private_enabled and _text(user.get("relationship_role"), 40).lower() == "owner":
                proactive_enabled = True
        proactive_enabled = bool(proactive_enabled)
        normalized = normalize_capabilities(
            {
                "private_companion_enabled": private_enabled,
                "proactive_private_enabled": proactive_enabled,
                "portrait_mode": "disabled",
                "grant_source": "legacy_effective_migration",
            },
            default_source="legacy_effective_migration",
        )
    normalized["private_companion_enabled"] = True
    user["unified_profile_capabilities"] = normalized
    user["private_companion_enabled"] = normalized["private_companion_enabled"]
    user["proactive_private_enabled"] = normalized["proactive_private_enabled"]
    user["enabled"] = normalized["private_companion_enabled"]
    return normalized


def capability_summary(
    user: Any,
    *,
    global_portrait_mode: str = "disabled",
    portrait_backend_available: bool = True,
) -> dict[str, Any]:
    source = user if isinstance(user, dict) else {}
    capabilities = source.get("unified_profile_capabilities")
    if not isinstance(capabilities, dict):
        capabilities = default_capabilities()
    resolved = dict(capabilities)
    if resolved.get("portrait_mode_override") != "explicit":
        resolved["portrait_mode"] = normalize_portrait_mode(global_portrait_mode)
    # Portrait facts belong to MemoryCompanion.  Do not leave a misleading
    # enabled state behind when the optional backend is absent or incompatible.
    if not portrait_backend_available:
        resolved["portrait_mode"] = "disabled"
        resolved["portrait_mode_override"] = "explicit"
        resolved["portrait_backend_code"] = "memory_companion_required"
    return build_capability_summary(resolved)


def private_companion_gate(user: Any, configured_reply: Any = "") -> dict[str, Any]:
    summary = capability_summary(user)
    return {"allowed": True, "code": "private_chat_unrestricted", "reply": "", "capabilities": summary}


def proactive_private_gate(user: Any) -> dict[str, Any]:
    summary = capability_summary(user)
    if not summary["proactive_private_enabled"]:
        return {"allowed": False, "code": "proactive_private_disabled", "capabilities": summary}
    return {"allowed": True, "code": "profile_exact", "capabilities": summary}


def update_capabilities(
    user: dict[str, Any],
    changes: dict[str, Any],
    *,
    actor_authorized: bool,
    grant_source: str = "admin",
    actor_id: str = "administrator",
    target_identity: str = "",
    reason_code: str = "administrator_update",
) -> dict[str, Any]:
    if not isinstance(user, dict) or not isinstance(changes, dict):
        return {"ok": False, "code": "invalid_request", "capabilities": default_capabilities()}
    if not actor_authorized:
        return {"ok": False, "code": "admin_required", "capabilities": capability_summary(user)}
    current = normalize_capabilities(user.get("unified_profile_capabilities"), default_source="default_closed")
    current["private_companion_enabled"] = True
    previous = {
        key: current.get(key)
        for key in ("private_companion_enabled", "proactive_private_enabled", "portrait_mode", "portrait_mode_override")
    }
    if "proactive_private_enabled" in changes:
        current["proactive_private_enabled"] = _bool(changes["proactive_private_enabled"])
    if "portrait_mode" in changes:
        requested_mode = _text(changes.get("portrait_mode"), 40).lower()
        if requested_mode == "follow_global":
            current["portrait_mode_override"] = "follow_global"
        else:
            current["portrait_mode"] = normalize_portrait_mode(requested_mode)
            current["portrait_mode_override"] = "explicit"
    changed = {
        key: {"from": previous.get(key), "to": current.get(key)}
        for key in previous
        if previous.get(key) != current.get(key)
    }
    if "proactive_private_enabled" in changed:
        current["grant_source"] = _text(grant_source, 80) or "admin"
    if changed:
        current["updated_at"] = _now()
    user["unified_profile_capabilities"] = current
    user["private_companion_enabled"] = current["private_companion_enabled"]
    user["proactive_private_enabled"] = current["proactive_private_enabled"]
    # Keep the historical aggregate field compatible without treating it as a
    # future authority source.
    user["enabled"] = current["private_companion_enabled"]
    if changed:
        audit = user.setdefault("unified_profile_capability_audit", [])
        if not isinstance(audit, list):
            audit = []
            user["unified_profile_capability_audit"] = audit
        audit.append(
            {
                "operation_id": f"req036.capability:{_now()}:{len(audit) + 1}",
                "actor_id": _text(actor_id, 120) or "administrator",
                "target_identity": _text(target_identity, 120),
                "reason_code": _text(reason_code, 80) or "administrator_update",
                "changed": changed,
                "at": _now(),
            }
        )
        del audit[:-64]
    return {"ok": True, "code": "updated", "capabilities": capability_summary(user)}


def migration_preview(data: Any, *, operation_id: str = "") -> dict[str, Any]:
    root = data if isinstance(data, dict) else {}
    users = root.get("users") if isinstance(root.get("users"), dict) else {}
    planned: list[dict[str, Any]] = []
    for user_id, user in users.items():
        if not isinstance(user, dict):
            continue
        existing = user.get("unified_profile_capabilities")
        if isinstance(existing, dict):
            try:
                if int(existing.get("schema_version") or 0) == CAPABILITY_SCHEMA_VERSION:
                    continue
            except (TypeError, ValueError, OverflowError):
                pass
        legacy_private_enabled = _legacy_private_enabled(user)
        private_enabled = True
        proactive_enabled = _legacy_proactive_enabled(user, legacy_private_enabled)
        planned.append(
            {
                "user_id": _text(user_id, 120),
                "private_companion_enabled": private_enabled,
                "proactive_private_enabled": proactive_enabled,
                "portrait_mode": "disabled",
                "grant_source": "legacy_effective_migration",
            }
        )
    return {
        "ok": True,
        "code": "migration_dry_run",
        "operation_id": _text(operation_id, 120),
        "write_count": 0,
        "planned": planned,
        "count": len(planned),
    }


def migrate_legacy_capabilities(data: dict[str, Any], *, operation_id: str, dry_run: bool = True) -> dict[str, Any]:
    operation_id = _text(operation_id, 120)
    if not isinstance(data, dict) or not operation_id:
        return {"ok": False, "code": "invalid_request", "count": 0}
    preview = migration_preview(data, operation_id=operation_id)
    if dry_run:
        return preview
    migration = data.get(MIGRATION_KEY)
    if not isinstance(migration, dict):
        migration = {}
        data[MIGRATION_KEY] = migration
    operations = migration.get("operations")
    if not isinstance(operations, dict):
        operations = {}
        migration["operations"] = operations
    prior_exists = operation_id in operations
    prior = operations.get(operation_id)
    recovered_corrupt_operation = False
    if prior_exists:
        if isinstance(prior, dict) and _migration_snapshots_valid(prior.get("snapshots")):
            snapshots = prior.get("snapshots")
            replay_count = len(snapshots)
            migration["version"] = CAPABILITY_SCHEMA_VERSION
            if type(prior.get("count")) is not int or prior.get("count") != replay_count:
                prior["count"] = replay_count
            return {
                "ok": True,
                "code": "migration_idempotent_replay",
                "operation_id": operation_id,
                "count": replay_count,
            }
        if not preview["planned"]:
            return {
                "ok": False,
                "code": "migration_corrupt",
                "operation_id": operation_id,
                "count": 0,
            }
        # A broken wrapper cannot prove that any migration was applied.  When
        # legacy users still exist, replacing it is safe because the loop
        # below snapshots and writes only profiles that still need migration.
        operations.pop(operation_id, None)
        recovered_corrupt_operation = True
    migration["version"] = CAPABILITY_SCHEMA_VERSION
    users = data.get("users") if isinstance(data.get("users"), dict) else {}
    snapshots: dict[str, Any] = {}
    for raw_user_id, user in users.items():
        if not isinstance(raw_user_id, str) or not raw_user_id or not isinstance(user, dict):
            continue
        existing = user.get("unified_profile_capabilities")
        if isinstance(existing, dict):
            try:
                if int(existing.get("schema_version") or 0) == CAPABILITY_SCHEMA_VERSION:
                    continue
            except (TypeError, ValueError, OverflowError):
                pass
        legacy_private_enabled = _legacy_private_enabled(user)
        private_enabled = True
        proactive_enabled = _legacy_proactive_enabled(user, legacy_private_enabled)
        snapshots[raw_user_id] = {
            "unified_profile_capabilities": deepcopy(user.get("unified_profile_capabilities")),
            "private_companion_enabled": deepcopy(user.get("private_companion_enabled")),
            "proactive_private_enabled": deepcopy(user.get("proactive_private_enabled")),
            "enabled": deepcopy(user.get("enabled")),
            "_present_fields": [key for key in _MIGRATION_SNAPSHOT_FIELDS if key in user],
        }
        state = normalize_capabilities(
            {
                "private_companion_enabled": private_enabled,
                "proactive_private_enabled": proactive_enabled,
                "portrait_mode": "disabled",
                "grant_source": "legacy_effective_migration",
            },
            default_source="legacy_effective_migration",
        )
        user["unified_profile_capabilities"] = state
        user["private_companion_enabled"] = state["private_companion_enabled"]
        user["proactive_private_enabled"] = state["proactive_private_enabled"]
    operations[operation_id] = {"count": len(snapshots), "snapshots": snapshots, "at": _now()}
    return {
        "ok": True,
        "code": "migration_applied",
        "operation_id": operation_id,
        "count": len(snapshots),
        "recovered_corrupt_operation": recovered_corrupt_operation,
    }


def default_closed_repair_preview(data: Any, *, operation_id: str = "") -> dict[str, Any]:
    root = data if isinstance(data, dict) else {}
    users = root.get("users") if isinstance(root.get("users"), dict) else {}
    migration = root.get(MIGRATION_KEY) if isinstance(root.get(MIGRATION_KEY), dict) else {}
    operations = migration.get("operations") if isinstance(migration.get("operations"), dict) else {}
    legacy_operation = operations.get("req036-capability-v1")
    legacy_snapshots = (
        legacy_operation.get("snapshots")
        if isinstance(legacy_operation, dict) and isinstance(legacy_operation.get("snapshots"), dict)
        else {}
    )
    planned: list[dict[str, Any]] = []
    for raw_user_id, user in users.items():
        if not isinstance(raw_user_id, str) or not raw_user_id or not isinstance(user, dict):
            continue
        repair = _default_closed_repair_plan(
            user,
            legacy_snapshot=legacy_snapshots.get(raw_user_id),
        )
        if repair is None:
            continue
        planned.append({"user_id": raw_user_id, **repair})
    return {
        "ok": True,
        "code": "default_closed_repair_dry_run",
        "operation_id": _text(operation_id, 120),
        "write_count": 0,
        "planned": planned,
        "count": len(planned),
    }


def repair_default_closed_capabilities(
    data: dict[str, Any],
    *,
    operation_id: str = DEFAULT_CLOSED_REPAIR_OPERATION_ID,
    dry_run: bool = True,
) -> dict[str, Any]:
    operation_id = _text(operation_id, 120)
    if not isinstance(data, dict) or not operation_id:
        return {"ok": False, "code": "invalid_request", "count": 0}
    preview = default_closed_repair_preview(data, operation_id=operation_id)
    if dry_run:
        return preview

    migration = data.get(MIGRATION_KEY)
    if not isinstance(migration, dict):
        migration = {}
        data[MIGRATION_KEY] = migration
    operations = migration.get("operations")
    if not isinstance(operations, dict):
        operations = {}
        migration["operations"] = operations
    prior_exists = operation_id in operations
    prior = operations.get(operation_id)
    recovered_corrupt_operation = False
    if prior_exists:
        if isinstance(prior, dict) and _migration_snapshots_valid(prior.get("snapshots")):
            snapshots = prior["snapshots"]
            replay_count = len(snapshots)
            migration["version"] = CAPABILITY_SCHEMA_VERSION
            if type(prior.get("count")) is not int or prior.get("count") != replay_count:
                prior["count"] = replay_count
            return {
                "ok": True,
                "code": "migration_idempotent_replay",
                "operation_id": operation_id,
                "count": replay_count,
            }
        if not preview["planned"]:
            return {
                "ok": False,
                "code": "migration_corrupt",
                "operation_id": operation_id,
                "count": 0,
            }
        operations.pop(operation_id, None)
        recovered_corrupt_operation = True

    migration["version"] = CAPABILITY_SCHEMA_VERSION
    users = data.get("users") if isinstance(data.get("users"), dict) else {}
    legacy_operation = operations.get("req036-capability-v1")
    legacy_snapshots = (
        legacy_operation.get("snapshots")
        if isinstance(legacy_operation, dict) and isinstance(legacy_operation.get("snapshots"), dict)
        else {}
    )
    snapshots: dict[str, Any] = {}
    for raw_user_id, user in users.items():
        if not isinstance(raw_user_id, str) or not raw_user_id or not isinstance(user, dict):
            continue
        repair = _default_closed_repair_plan(
            user,
            legacy_snapshot=legacy_snapshots.get(raw_user_id),
        )
        if repair is None:
            continue
        snapshots[raw_user_id] = {
            "unified_profile_capabilities": deepcopy(user.get("unified_profile_capabilities")),
            "private_companion_enabled": deepcopy(user.get("private_companion_enabled")),
            "proactive_private_enabled": deepcopy(user.get("proactive_private_enabled")),
            "enabled": deepcopy(user.get("enabled")),
            "_present_fields": [key for key in _MIGRATION_SNAPSHOT_FIELDS if key in user],
        }
        state = normalize_capabilities(user.get("unified_profile_capabilities"))
        state["private_companion_enabled"] = repair["private_companion_enabled"]
        state["proactive_private_enabled"] = repair["proactive_private_enabled"]
        state["grant_source"] = repair["grant_source"]
        state["updated_at"] = _now()
        user["unified_profile_capabilities"] = state
        user["private_companion_enabled"] = state["private_companion_enabled"]
        user["proactive_private_enabled"] = state["proactive_private_enabled"]
        user["enabled"] = state["private_companion_enabled"]
    operations[operation_id] = {"count": len(snapshots), "snapshots": snapshots, "at": _now()}
    return {
        "ok": True,
        "code": "default_closed_repair_applied",
        "operation_id": operation_id,
        "count": len(snapshots),
        "recovered_corrupt_operation": recovered_corrupt_operation,
    }


def rollback_legacy_capabilities(data: dict[str, Any], *, operation_id: str) -> dict[str, Any]:
    operation_id = _text(operation_id, 120)
    migration = data.get(MIGRATION_KEY) if isinstance(data, dict) else None
    operations = migration.get("operations") if isinstance(migration, dict) else None
    operation = operations.get(operation_id) if isinstance(operations, dict) else None
    if not isinstance(operation, dict):
        return {"ok": False, "code": "migration_not_found", "count": 0}
    users = data.get("users") if isinstance(data.get("users"), dict) else {}
    restored = 0
    snapshots = operation.get("snapshots")
    if not isinstance(snapshots, dict):
        return {"ok": False, "code": "migration_corrupt", "operation_id": operation_id, "count": 0}
    validated: list[tuple[dict[str, Any], dict[str, Any], set[str] | None]] = []
    for user_id, snapshot in snapshots.items():
        if not isinstance(user_id, str) or not isinstance(snapshot, dict):
            return {"ok": False, "code": "migration_corrupt", "operation_id": operation_id, "count": 0}
        snapshot_valid, exact_presence = _migration_snapshot_presence(snapshot)
        if not snapshot_valid:
            return {"ok": False, "code": "migration_corrupt", "operation_id": operation_id, "count": 0}
        user = users.get(user_id)
        if not isinstance(user, dict):
            continue
        validated.append((user, snapshot, exact_presence))
    for user, snapshot, exact_presence in validated:
        for key, value in snapshot.items():
            if key == "_present_fields":
                continue
            if exact_presence is not None and key not in exact_presence:
                user.pop(key, None)
            elif exact_presence is not None:
                user[key] = deepcopy(value)
            elif value is None:
                # Compatibility with migration records created before exact
                # field-presence snapshots were introduced.
                user.pop(key, None)
            else:
                user[key] = deepcopy(value)
        restored += 1
    operation["rolled_back_at"] = _now()
    return {"ok": True, "code": "migration_rolled_back", "operation_id": operation_id, "count": restored}


__all__ = [name for name in globals() if name.isupper() or name in {
    "capability_summary", "default_capabilities", "default_closed_repair_preview", "ensure_legacy_profile_capabilities", "ensure_new_profile_capabilities",
    "migrate_legacy_capabilities", "migration_preview", "normalize_capabilities", "private_companion_gate",
    "proactive_private_gate", "repair_default_closed_capabilities", "rollback_legacy_capabilities",
    "update_capabilities",
}]
