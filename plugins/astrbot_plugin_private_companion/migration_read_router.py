"""REQ-041 per-message relationship read generation router."""
from __future__ import annotations

import hashlib
import time
from typing import Any

try:
    from .identity_namespace import build_namespace_context
    from .migration_coordinator import MigrationCoordinator
    from .relationship_account_store import RelationshipAccountStore
    from .unified_person_registry import UnifiedPersonRegistry
except ImportError:  # pragma: no cover - direct-module test compatibility
    from identity_namespace import build_namespace_context
    from migration_coordinator import MigrationCoordinator
    from relationship_account_store import RelationshipAccountStore
    from unified_person_registry import UnifiedPersonRegistry


class MigrationReadError(RuntimeError):
    pass


class MigrationRelationshipReadRouter:
    def __init__(
        self,
        *,
        coordinator: MigrationCoordinator,
        relationship_store: RelationshipAccountStore,
        registry_resolver: Any,
        migration_epoch: str,
        policy_version: str,
        observability: Any = None,
    ) -> None:
        self.coordinator = coordinator
        self.relationship_store = relationship_store
        self.registry_resolver = registry_resolver if callable(registry_resolver) else None
        self.migration_epoch = str(migration_epoch or "").strip()
        self.policy_version = str(policy_version or "").strip()
        self.observability = observability
        if self.registry_resolver is None or not self.migration_epoch or not self.policy_version:
            raise MigrationReadError("migration_read_contract_invalid")

    def _registry(self, person_id: str) -> UnifiedPersonRegistry:
        registry = self.registry_resolver(person_id)
        if not isinstance(registry, UnifiedPersonRegistry):
            raise MigrationReadError("migration_read_registry_missing")
        return registry

    def _chain_id(self, person_id: str, event_ref: str) -> str:
        reference = str(event_ref or "").strip()
        if not reference:
            raise MigrationReadError("migration_read_event_ref_missing")
        digest = hashlib.sha256(
            f"{self.migration_epoch}:{person_id}:{reference}".encode("utf-8")
        ).hexdigest()
        return f"req041-chain-{digest}"

    def begin(
        self,
        user: dict[str, Any],
        *,
        event_ref: str,
        kind: str = "private",
        group_id: str = "",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        if not isinstance(user, dict):
            if self.observability is not None:
                self.observability.increment("runtime_error")
            return {"generation": "legacy", "chain_id": "", "user": user, "code": "legacy_user_invalid"}
        person_id = str(user.get("unified_person_id") or "").strip()
        if not person_id:
            return {"generation": "legacy", "chain_id": "", "user": dict(user), "code": "identity_pending"}
        generation = "legacy"
        try:
            registry = self._registry(person_id)
            subject = str(user.get("identity_subject_id") or user.get("user_id") or "").strip()
            if not subject or not registry.matches_person_subject(person_id, subject):
                raise MigrationReadError("migration_read_subject_mismatch")
            chain_id = self._chain_id(person_id, event_ref)
            generation = self.coordinator.begin_read_chain(person_id, chain_id)
            if generation != "new":
                return {
                    "generation": "legacy", "chain_id": chain_id,
                    "identity_id": person_id, "user": dict(user), "code": "legacy_generation",
                }
            resolution = registry.formal_namespace_for_person(
                person_id, kind=kind, group_id=group_id,
                policy_version=self.policy_version, migration_epoch=self.migration_epoch,
                purpose="relationship_read",
            )
            context = build_namespace_context(resolution.get("context") if isinstance(resolution, dict) else None)
            if context is None or not resolution.get("ok") or context.errors():
                raise MigrationReadError("migration_read_namespace_invalid")
            view = dict(user)
            if kind == "private":
                account = self.relationship_store.account(context)
                comparisons = {
                    "relationship_role": account["relationship_role"],
                    "relationship_mode": account["relationship_mode"],
                    "relationship_score": account["relationship_score"],
                    "relationship_positive_stage_cap_key": account["relationship_positive_stage_cap_key"],
                }
                if any(
                    key in user and user.get(key) is not None and str(user.get(key)) != str(value)
                    for key, value in comparisons.items()
                ):
                    if self.observability is not None:
                        self.observability.increment("shadow_read_mismatch")
                    raise MigrationReadError("migration_read_shadow_mismatch")
                view.update({
                    "relationship_role": account["relationship_role"],
                    "relationship_mode": account["relationship_mode"],
                    "relationship_score": account["relationship_score"],
                    "relationship_positive_stage_cap_key": account["relationship_positive_stage_cap_key"],
                    "relationship_phase_key": account["relationship_stage_key"],
                })
            else:
                summary = self.relationship_store.summary(context)
                view.update({
                    "relationship_role": summary["relationship_role"],
                    "relationship_mode": summary["relationship_mode"],
                    "relationship_score": 0,
                    "relationship_phase_key": summary["stage_key"],
                    "req041_relationship_stage_key": summary["stage_key"],
                })
            view["req041_read_generation"] = "new"
            if self.observability is not None:
                self.observability.observe(
                    "permission_profile_relationship", (time.perf_counter() - started) * 1000.0,
                )
            return {
                "generation": "new", "chain_id": chain_id,
                "identity_id": person_id, "user": view, "code": "new_generation",
            }
        except Exception as exc:
            code = str(exc).split(":", 1)[0] or "migration_read_failed"
            rolled_back = generation == "new"
            if person_id and rolled_back:
                try:
                    self.coordinator.rollback_identity(person_id, reason_code=code[:80])
                    if self.observability is not None:
                        self.observability.increment("identity_rollback")
                except Exception:
                    pass
            chain_id = locals().get("chain_id", "")
            if chain_id:
                try:
                    self.coordinator.finish_read_chain(chain_id)
                except Exception:
                    pass
            return {
                "generation": "legacy", "chain_id": "", "identity_id": person_id,
                "user": dict(user), "code": code[:80], "rolled_back": rolled_back,
            }

    def finish(self, chain_id: str) -> bool:
        return self.coordinator.finish_read_chain(chain_id) if str(chain_id or "").strip() else False


__all__ = ["MigrationReadError", "MigrationRelationshipReadRouter"]
