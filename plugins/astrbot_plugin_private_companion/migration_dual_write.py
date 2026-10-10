"""REQ-041 fail-closed online Shadow dual-write producers."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

try:
    from .identity_namespace import build_namespace_context
    from .migration_coordinator import MigrationCoordinator
    from .migration_outbox import MigrationOutbox
    from .unified_person_registry import UnifiedPersonRegistry
except ImportError:  # pragma: no cover - direct-module test compatibility
    from identity_namespace import build_namespace_context
    from migration_coordinator import MigrationCoordinator
    from migration_outbox import MigrationOutbox
    from unified_person_registry import UnifiedPersonRegistry


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _token(value: Any, limit: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        return ""
    return text


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _relationship_runtime_state(user: dict[str, Any]) -> dict[str, Any]:
    raw_totals = user.get("relationship_daily_totals")
    if raw_totals is not None and not isinstance(raw_totals, dict):
        raise MigrationDualWriteError("dual_write_relationship_runtime_invalid")
    totals = raw_totals if isinstance(raw_totals, dict) else {}
    day = _token(totals.get("day"), 16)
    positive = _integer(totals.get("positive", 0))
    negative = _integer(totals.get("negative", 0))
    try:
        last_effective = float(user.get("relationship_last_effective_at") or 0.0)
    except (TypeError, ValueError, OverflowError):
        raise MigrationDualWriteError("dual_write_relationship_runtime_invalid")
    if (
        positive is None or negative is None or not 0 <= positive <= 120
        or not -180 <= negative <= 0 or not math.isfinite(last_effective) or last_effective < 0
    ):
        raise MigrationDualWriteError("dual_write_relationship_runtime_invalid")
    return {
        "positive_stage_cap_key": _token(user.get("relationship_positive_stage_cap_key"), 40) or "deeply_bonded",
        "daily_totals": {
            "day": day,
            "positive": positive,
            "negative": negative,
        },
        "last_effective_at": last_effective,
    }


class MigrationDualWriteError(RuntimeError):
    pass


class MigrationDualWriteProducer:
    def __init__(
        self,
        *,
        outbox: MigrationOutbox,
        coordinator: MigrationCoordinator,
        migration_epoch: str,
        policy_version: str,
        on_enqueued: Any = None,
    ) -> None:
        self.outbox = outbox
        self.coordinator = coordinator
        self.migration_epoch = _token(migration_epoch)
        self.policy_version = _token(policy_version, 64)
        self.on_enqueued = on_enqueued if callable(on_enqueued) else None
        if not self.migration_epoch or not self.policy_version:
            raise MigrationDualWriteError("dual_write_contract_invalid")

    def _notify(self, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("status") == "enqueued" and self.on_enqueued is not None:
            self.on_enqueued()
        return result

    def _pending(self, user: dict[str, Any], reason: str, source_scope: str) -> None:
        raw_reference = str(user.get("unified_person_id") or user.get("user_id") or "missing")
        opaque = hashlib.sha256(
            f"{self.migration_epoch}:dual-write:{source_scope}:{raw_reference}".encode("utf-8")
        ).hexdigest()
        self.coordinator.record_pending(
            opaque,
            source_kind="relationship_write",
            reason_code=reason,
        )

    def _relationship_context(
        self,
        registry: UnifiedPersonRegistry,
        user: dict[str, Any],
        source_scope: str,
    ) -> tuple[Any | None, str]:
        if bool(user.get("observation_only")) or str(user.get("profile_origin") or "") == "group_observation":
            self._pending(user, "group_observation_relationship_denied", source_scope)
            return None, "group_observation_relationship_denied"
        person_id = _token(user.get("unified_person_id"))
        if not person_id:
            self._pending(user, "relationship_identity_pending", source_scope)
            return None, "relationship_identity_pending"
        subject_id = _token(user.get("identity_subject_id"), 160) or _token(user.get("user_id"), 160)
        if not subject_id or not registry.matches_person_subject(person_id, subject_id):
            self._pending(user, "relationship_identity_subject_mismatch", source_scope)
            return None, "relationship_identity_subject_mismatch"
        resolution = registry.formal_namespace_for_person(
            person_id,
            kind="private",
            policy_version=self.policy_version,
            migration_epoch=self.migration_epoch,
            purpose="relationship_write",
        )
        context_payload = resolution.get("context") if isinstance(resolution, dict) else None
        if not resolution.get("ok") or not isinstance(context_payload, dict):
            self._pending(user, "relationship_identity_not_formal", source_scope)
            return None, "relationship_identity_not_formal"
        context = build_namespace_context(context_payload)
        if context is None or context.errors():
            raise MigrationDualWriteError("dual_write_namespace_invalid")
        return context, ""

    def emit_relationship(
        self,
        *,
        registry: UnifiedPersonRegistry,
        user: dict[str, Any],
        requested_delta: int,
        reason_code: str,
        result: dict[str, Any],
        source_scope: str = "default",
        source_revision: int,
        group_admission_event_id: str = "",
    ) -> dict[str, Any]:
        if not isinstance(user, dict) or not isinstance(result, dict):
            raise MigrationDualWriteError("dual_write_relationship_invalid")
        if not result.get("changed"):
            return {"status": "skipped", "code": "legacy_event_unchanged"}
        context, denied = self._relationship_context(registry, user, source_scope)
        if context is None:
            return {"status": "skipped", "code": denied}
        person_id = context.identity_id
        entry = result.get("entry")
        if not isinstance(entry, dict):
            raise MigrationDualWriteError("dual_write_relationship_entry_missing")
        event_key = _token(entry.get("event_key"), 80)
        reason = _token(reason_code, 48)
        requested = _integer(requested_delta)
        applied = _integer(result.get("delta"))
        score_before = _integer(entry.get("score_before"))
        score_after = _integer(entry.get("score_after"))
        if not event_key or not reason or None in {requested, applied, score_before, score_after}:
            raise MigrationDualWriteError("dual_write_relationship_entry_invalid")
        admission_ref = _token(group_admission_event_id, 128)
        if reason == "direct_group_interaction" and not admission_ref:
            raise MigrationDualWriteError("dual_write_group_admission_missing")
        if reason != "direct_group_interaction" and admission_ref:
            raise MigrationDualWriteError("dual_write_group_admission_unexpected")
        role = "owner" if str(user.get("relationship_role") or "").strip().lower() == "owner" else "friend"
        mode = "owner_exclusive" if role == "owner" and str(user.get("relationship_mode") or "").strip().lower() == "owner_exclusive" else "normal"
        payload = {
            "operation": "relationship_legacy_event",
            "identity_ref": person_id,
            "event_key": event_key,
            "reason_code": reason,
            "requested_delta": requested,
            "applied_delta": applied,
            "score_before": score_before,
            "score_after": score_after,
            "group_admission_event_ref": admission_ref,
            "relationship_role": role,
            "relationship_mode": mode,
            **_relationship_runtime_state(user),
            "legacy_event_hash": hashlib.sha256(
                _canonical({
                    "event_key": event_key,
                    "reason_code": reason,
                    "applied_delta": applied,
                    "score_before": score_before,
                    "score_after": score_after,
                    "group_admission_event_ref": admission_ref,
                }).encode("utf-8")
            ).hexdigest(),
        }
        expected_revision = _integer(source_revision)
        if expected_revision is None or expected_revision < 1:
            raise MigrationDualWriteError("dual_write_source_revision_invalid")
        event_id = "req041-rel-" + hashlib.sha256(
            f"{self.migration_epoch}:{person_id}:{event_key}".encode("utf-8")
        ).hexdigest()[:40]
        emitted = self.outbox.enqueue_next(
            stream_key=f"relationship:{person_id}",
            event_id=event_id,
            namespace=context,
            migration_epoch=self.migration_epoch,
            policy_version=self.policy_version,
            payload=payload,
        )
        return self._notify(emitted)

    def emit_relationship_snapshot(
        self,
        *,
        registry: UnifiedPersonRegistry,
        user: dict[str, Any],
        reason_code: str,
        source_scope: str = "default",
        source_revision: int,
    ) -> dict[str, Any]:
        if not isinstance(user, dict):
            raise MigrationDualWriteError("dual_write_relationship_invalid")
        context, denied = self._relationship_context(registry, user, source_scope)
        if context is None:
            return {"status": "skipped", "code": denied}
        reason = _token(reason_code, 64)
        score = _integer(user.get("relationship_score"))
        if not reason or score is None or not -1200 <= score <= 1200:
            raise MigrationDualWriteError("dual_write_relationship_snapshot_invalid")
        role = "owner" if str(user.get("relationship_role") or "").strip().lower() == "owner" else "friend"
        mode = "owner_exclusive" if role == "owner" and str(user.get("relationship_mode") or "").strip().lower() == "owner_exclusive" else "normal"
        runtime_state = _relationship_runtime_state(user)
        state = {
            "relationship_role": role,
            "relationship_mode": mode,
            "relationship_score": score,
            **runtime_state,
        }
        state_hash = hashlib.sha256(_canonical(state).encode("utf-8")).hexdigest()
        ledger = user.get("relationship_ledger")
        latest = ledger[-1] if isinstance(ledger, list) and ledger and isinstance(ledger[-1], dict) else {}
        anchor = _token(latest.get("event_key"), 80)
        if not anchor:
            anchor = hashlib.sha256(
                _canonical({
                    "reason_code": latest.get("reason_code"),
                    "delta": latest.get("delta"),
                    "score_after": latest.get("score_after"),
                    "state_hash": state_hash,
                }).encode("utf-8")
            ).hexdigest()[:24]
        payload = {
            "operation": "relationship_legacy_snapshot",
            "identity_ref": context.identity_id,
            **state,
            "snapshot_hash": state_hash,
            "reason_code": reason,
            "legacy_event_ref": anchor,
        }
        expected_revision = _integer(source_revision)
        if expected_revision is None or expected_revision < 1:
            raise MigrationDualWriteError("dual_write_source_revision_invalid")
        event_id = "req041-rel-snapshot-" + hashlib.sha256(
            f"{self.migration_epoch}:{context.identity_id}:{reason}:{anchor}:{state_hash}".encode("utf-8")
        ).hexdigest()[:32]
        emitted = self.outbox.enqueue_next(
            stream_key=f"relationship:{context.identity_id}",
            event_id=event_id,
            namespace=context,
            migration_epoch=self.migration_epoch,
            policy_version=self.policy_version,
            payload=payload,
        )
        return self._notify(emitted)

    def emit_identity_change(
        self,
        *,
        registry: UnifiedPersonRegistry,
        result: dict[str, Any],
        action: str,
        operation_id: str,
    ) -> dict[str, Any]:
        if not isinstance(result, dict) or not result.get("ok") or not result.get("changed"):
            return {"status": "skipped", "code": "identity_unchanged"}
        clean_action = _token(action, 32)
        if clean_action not in {"create", "link", "unlink"}:
            raise MigrationDualWriteError("dual_write_identity_action_invalid")
        person_id = _token(result.get("person_id"))
        identity_key = _token(result.get("identity_key"), 160)
        if not person_id or not identity_key:
            raise MigrationDualWriteError("dual_write_identity_result_invalid")
        resolution = registry.formal_namespace_for_person(
            person_id,
            kind="private",
            policy_version=self.policy_version,
            migration_epoch=self.migration_epoch,
            purpose="relationship_write",
        )
        context_payload = resolution.get("context") if isinstance(resolution, dict) else None
        context = build_namespace_context(context_payload)
        if not resolution.get("ok") or context is None or context.errors():
            raise MigrationDualWriteError("dual_write_identity_not_formal")
        projection = result.get("projection") if isinstance(result.get("projection"), dict) else {}
        if not projection and clean_action == "unlink":
            projection = registry.read_projection(person_id) or {}
        revision = _integer(projection.get("projection_revision"))
        if revision is None or revision < 1:
            raise MigrationDualWriteError("dual_write_identity_revision_invalid")
        checkpoint = registry.identity_projection_checkpoint(person_id)
        if (
            not checkpoint.get("ok")
            or int(checkpoint.get("projection_revision") or 0) != revision
            or not _token(checkpoint.get("checkpoint_hash"), 80)
        ):
            raise MigrationDualWriteError("dual_write_identity_checkpoint_invalid")
        self.coordinator.register_identity(person_id, assurance=context.assurance)
        target_assurance = (
            "explicit_linked"
            if projection.get("identity_assurance") == "explicit_linked"
            else context.assurance
        )
        payload = {
            "operation": f"identity_{clean_action}",
            "identity_ref": person_id,
            "identity_key_ref": identity_key,
            "identity_assurance": target_assurance,
            "profile_status": context.profile_status,
            "projection_revision": revision,
            "projection_checkpoint_hash": checkpoint["checkpoint_hash"],
        }
        operation_hash = hashlib.sha256(str(operation_id or "").encode("utf-8")).hexdigest()[:24]
        event_id = "req041-id-" + hashlib.sha256(
            f"{self.migration_epoch}:{clean_action}:{person_id}:{identity_key}:{operation_hash}".encode("utf-8")
        ).hexdigest()[:40]
        envelope = {
            "stream_key": f"identity:{person_id}",
            "event_id": event_id,
            "namespace": context,
            "migration_epoch": self.migration_epoch,
            "policy_version": self.policy_version,
            "payload": payload,
        }
        if clean_action == "unlink":
            return self._notify(self.outbox.enqueue_next_with_tombstone(
                **envelope,
                tombstone_key=f"identity-link:{identity_key}",
                reason_code="identity_unlink",
            ))
        return self._notify(self.outbox.enqueue_next(**envelope))

    def fail_closed(self, reason_code: str) -> None:
        reason = _token(reason_code, 80) or "dual_write_failed"
        self.coordinator.pause(reason)
        try:
            self.outbox.set_epoch_state(self.migration_epoch, "degraded", checkpoint=reason)
        except Exception:
            pass


__all__ = ["MigrationDualWriteError", "MigrationDualWriteProducer"]
