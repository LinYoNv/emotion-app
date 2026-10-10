"""REQ-041 fail-closed runtime bridge for admitted group affinity events."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

try:
    from .identity_namespace import NamespaceContext, build_namespace_context
    from .relationship_account_store import GroupAffinityAdmissionResult, RelationshipAccountStore
    from .relationship_event_policy import build_group_interaction_proof
except ImportError:  # pragma: no cover - direct-module test compatibility
    from identity_namespace import NamespaceContext, build_namespace_context
    from relationship_account_store import GroupAffinityAdmissionResult, RelationshipAccountStore
    from relationship_event_policy import build_group_interaction_proof


CANDIDATE_SCHEMA = "req041.group_affinity_candidate.v1"


def normalize_group_allowlist(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        items: Iterable[Any] = re.split(r"[\s,，、;；]+", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        items = ()
    return frozenset(
        text for item in items
        if (text := str(item or "").strip()) and len(text) <= 160
    )


def prepare_group_affinity_candidate(
    context: NamespaceContext,
    *,
    raw_group_id: str,
    allowlist: Any,
    enabled: bool,
    inbound_event_id: str,
    directed_by: str,
    legacy_user_key: str,
    inbound: bool,
    human_sender: bool,
    forwarded: bool,
    echo: bool,
    historical: bool,
) -> dict[str, Any] | None:
    raw_group = str(raw_group_id or "").strip()
    inbound_id = str(inbound_event_id or "").strip()
    direction = str(directed_by or "").strip().lower()
    user_key = str(legacy_user_key or "").strip()
    groups = normalize_group_allowlist(allowlist)
    if (
        enabled is not True
        or not groups
        or raw_group not in groups
        or context.kind != "group_member"
        or context.errors()
        or not inbound_id
        or len(inbound_id) > 160
        or direction not in {"at_bot", "reply_bot"}
        or not user_key
        or inbound is not True
        or human_sender is not True
        or forwarded is True
        or echo is True
        or historical is True
    ):
        return None
    event_id = "req041-group-affinity-" + hashlib.sha256(
        f"{context.migration_epoch}:{context.cache_scope()}:{inbound_id}".encode("utf-8")
    ).hexdigest()[:40]
    return {
        "schema": CANDIDATE_SCHEMA,
        "event_id": event_id,
        "context": context.to_dict(),
        "directed_by": direction,
        # This key is event-local compatibility routing only.  It is never
        # included in the proof, admission row, outbox payload or audit log.
        "legacy_user_key": user_key,
        "raw_group_id": raw_group,
        "inbound": True,
        "human_sender": True,
        "forwarded": False,
        "echo": False,
        "historical": False,
    }


def admit_confirmed_group_affinity(
    candidate: Any,
    store: RelationshipAccountStore,
    *,
    reply_succeeded: bool,
    requested_delta: int = 4,
    group_daily_net_cap: int = 2,
    group_window_seconds: int = 30 * 60,
    group_window_absolute_cap: int = 1,
    group_person_daily_absolute_cap: int = 4,
    group_scope_daily_absolute_cap: int = 20,
    group_event_cap: int = 4,
) -> GroupAffinityAdmissionResult | None:
    if (
        not isinstance(candidate, dict)
        or candidate.get("schema") != CANDIDATE_SCHEMA
        or reply_succeeded is not True
        or not isinstance(store, RelationshipAccountStore)
    ):
        return None
    context = build_namespace_context(candidate.get("context"))
    event_id = str(candidate.get("event_id") or "").strip()
    if context is None or context.kind != "group_member" or context.errors() or not event_id:
        return None
    proof = build_group_interaction_proof(
        context,
        event_id=event_id,
        directed_by=str(candidate.get("directed_by") or ""),
        inbound=candidate.get("inbound") is True,
        human_sender=candidate.get("human_sender") is True,
        bot_reply_succeeded=True,
        forwarded=candidate.get("forwarded") is True,
        echo=candidate.get("echo") is True,
        historical=candidate.get("historical") is True,
    )
    return store.admit_group_event(
        context,
        event_id=event_id,
        delta=requested_delta,
        weight=0.25,
        allow_group_affinity=True,
        group_daily_net_cap=group_daily_net_cap,
        group_window_seconds=group_window_seconds,
        group_window_absolute_cap=group_window_absolute_cap,
        group_person_daily_absolute_cap=group_person_daily_absolute_cap,
        group_scope_daily_absolute_cap=group_scope_daily_absolute_cap,
        group_event_cap=group_event_cap,
        group_interaction_proof=proof,
    )


__all__ = [
    "CANDIDATE_SCHEMA",
    "admit_confirmed_group_affinity",
    "normalize_group_allowlist",
    "prepare_group_affinity_candidate",
]
