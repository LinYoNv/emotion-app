"""Read-only consumer for the companion-owned Unified Person projection.

The memory plugin is deliberately not an authority for person creation or
identity linking.  This module accepts a projection produced by the companion
plugin, validates it against the shared contract, and returns only a small
safe reference suitable for memory retrieval/context decisions.
"""

from __future__ import annotations

from typing import Any

from .person_context_contract import (
    PROJECTION_SCHEMA_VERSION,
    CONTRACT_FINGERPRINT,
    resolve_identity,
    validate_projection,
)


_SAFE_REFERENCE_FIELDS = (
    "person_id",
    "resolved_identity_key",
    "projection_revision",
    "projection_schema_version",
    "contract_fingerprint",
    "identity_assurance",
    "profile_status",
    "owner_mode",
    "relation_policy_id",
    "relation_label",
    "affinity_band",
    "relationship_capabilities",
    "group_overlay_ref",
    "updated_at",
)


def _reference(projection: dict[str, Any]) -> dict[str, Any]:
    """Copy only contract-approved metadata; never return caller objects."""

    result: dict[str, Any] = {}
    for field in _SAFE_REFERENCE_FIELDS:
        value = projection.get(field)
        if field == "relationship_capabilities":
            result[field] = list(value) if isinstance(value, list) else []
        else:
            result[field] = value
    return result


def consume_person_projection(
    projection: Any,
    expected_identity_key: str = "",
    expected_person_id: str = "",
    *,
    companion_available: bool = True,
    identity_store: dict[str, Any] | None = None,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume a companion projection without creating or mutating anything.

    ``identity_store`` and ``identity`` are optional read-only verification
    inputs.  When both are supplied, the shared ``resolve_identity`` contract
    function is used and the resolved person/key must agree with the incoming
    projection.  Normal bridge calls only need the three positional arguments.
    """

    base = {"read_only": True, "source": "companion_projection"}
    if not companion_available:
        return {
            **base,
            "state": "degraded",
            "degraded": True,
            "errors": ["companion_unavailable"],
        }
    if not isinstance(projection, dict):
        return {**base, "state": "invalid", "errors": ["projection_invalid"]}

    errors = list(validate_projection(projection))
    if errors:
        return {**base, "state": "invalid", "errors": errors}

    person_id = str(projection.get("person_id") or "")
    identity_key = str(projection.get("resolved_identity_key") or "")
    if expected_person_id and person_id != expected_person_id:
        errors.append("person_id_mismatch")
    if expected_identity_key and identity_key != expected_identity_key:
        errors.append("identity_key_mismatch")

    if identity_store is not None or identity is not None:
        if not isinstance(identity_store, dict) or not isinstance(identity, dict):
            errors.append("identity_resolution_input_invalid")
        else:
            resolved = resolve_identity(identity_store, identity)
            if resolved.get("state") != "resolved":
                errors.append(f"identity_{resolved.get('state', 'invalid')}")
            else:
                if resolved.get("identity_key") != identity_key:
                    errors.append("identity_key_unresolved")
                if resolved.get("person_id") != person_id:
                    errors.append("person_id_unresolved")

    if errors:
        return {
            **base,
            "state": "invalid",
            "errors": errors,
            "projection_ref": {"person_id": person_id, "resolved_identity_key": identity_key},
        }

    return {
        **base,
        "state": "resolved",
        "degraded": False,
        "projection_ref": _reference(projection),
    }


def projection_capability(*, companion_available: bool = True) -> dict[str, Any]:
    """Return a database-free, read-only capability descriptor."""

    if not companion_available:
        return {
            "available": False,
            "state": "degraded",
            "degraded": True,
            "read_only": True,
            "reason": "companion_unavailable",
        }
    return {
        "available": True,
        "state": "ready",
        "degraded": False,
        "read_only": True,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "contract_fingerprint": CONTRACT_FINGERPRINT,
        "methods": ["consume_person_projection"],
    }


__all__ = ["consume_person_projection", "projection_capability"]
