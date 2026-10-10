"""REQ-041 strict relationship event admission and opaque group proof binding."""
from __future__ import annotations

import hashlib
import json
from typing import Any

try:
    from .identity_namespace import NamespaceContext
except ImportError:  # pragma: no cover - direct-module test compatibility
    from identity_namespace import NamespaceContext


GROUP_PROOF_SCHEMA = "req041.group_interaction_proof.v1"
GROUP_DIRECTED_BY = frozenset({"at_bot", "reply_bot"})
GROUP_PROOF_KEYS = frozenset({
    "schema",
    "event_binding",
    "directed_by",
    "inbound",
    "human_sender",
    "bot_reply_succeeded",
    "forwarded",
    "echo",
    "historical",
})


class RelationshipEventPolicyError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _token(value: Any, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        return ""
    return text


def group_event_binding(context: NamespaceContext, event_id: str) -> str:
    event = _token(event_id)
    if (
        not event
        or context.kind != "group_member"
        or not context.identity_id
        or not context.group_id
        or context.errors()
    ):
        raise RelationshipEventPolicyError("group_event_binding_invalid")
    payload = {
        "migration_epoch": context.migration_epoch,
        "policy_version": context.policy_version,
        "namespace": context.cache_scope(),
        "event_id": event,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def build_group_interaction_proof(
    context: NamespaceContext,
    *,
    event_id: str,
    directed_by: str,
    inbound: bool,
    human_sender: bool,
    bot_reply_succeeded: bool,
    forwarded: bool,
    echo: bool,
    historical: bool,
) -> dict[str, Any]:
    direction = _token(directed_by, 24).lower()
    if direction not in GROUP_DIRECTED_BY:
        raise RelationshipEventPolicyError("group_interaction_direction_invalid")
    return {
        "schema": GROUP_PROOF_SCHEMA,
        "event_binding": group_event_binding(context, event_id),
        "directed_by": direction,
        "inbound": inbound is True,
        "human_sender": human_sender is True,
        "bot_reply_succeeded": bot_reply_succeeded is True,
        "forwarded": forwarded is True,
        "echo": echo is True,
        "historical": historical is True,
    }


def validate_group_interaction_proof(
    proof: Any,
    context: NamespaceContext,
    *,
    event_id: str,
) -> tuple[bool, str]:
    if not isinstance(proof, dict) or set(proof) != GROUP_PROOF_KEYS:
        return False, "group_interaction_proof_invalid"
    if proof.get("schema") != GROUP_PROOF_SCHEMA:
        return False, "group_interaction_proof_schema_invalid"
    try:
        expected_binding = group_event_binding(context, event_id)
    except RelationshipEventPolicyError:
        return False, "group_interaction_proof_context_invalid"
    if proof.get("event_binding") != expected_binding:
        return False, "group_interaction_proof_binding_mismatch"
    if proof.get("directed_by") not in GROUP_DIRECTED_BY:
        return False, "group_interaction_direction_invalid"
    required_true = ("inbound", "human_sender", "bot_reply_succeeded")
    required_false = ("forwarded", "echo", "historical")
    if any(proof.get(key) is not True for key in required_true):
        return False, "group_interaction_proof_incomplete"
    if any(proof.get(key) is not False for key in required_false):
        return False, "group_interaction_source_denied"
    return True, "group_interaction_proof_verified"


__all__ = [
    "GROUP_PROOF_SCHEMA",
    "RelationshipEventPolicyError",
    "build_group_interaction_proof",
    "group_event_binding",
    "validate_group_interaction_proof",
]
