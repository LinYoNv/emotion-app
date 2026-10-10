# -*- coding: utf-8 -*-
"""Deterministic disclosure firewall for canonical C3 agenda records.

The policy is intentionally independent from the reconciliation writer.  It
computes a purpose-specific view from already stored agenda candidates and
never upgrades a plan to an execution fact merely because its time has passed.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator, Mapping

try:
    from .schedule_authority import ScheduleAuthorityAdapter, validate_structured_schedule_ref
except ImportError:
    from schedule_authority import ScheduleAuthorityAdapter, validate_structured_schedule_ref

try:  # package import
    from .bot_personal_contract import BOT_PERSONAL_CANONICAL_SCHEMA_VERSION
    from .agenda_contracts import (
        ACTOR_TYPES,
        AGENDA_STATUSES,
        AUTHORITY_KINDS,
        COMMITMENT_LEVELS,
        CONTENT_GRANULARITIES,
        EPISTEMIC_STATUSES,
        EVIDENCE_KINDS,
        FACT_ELIGIBILITIES,
        MATERIALIZATION_STATES,
        TEMPORAL_PHASES,
        derive_temporal_phase,
        normalize_authority_kind,
        normalize_commitment_level,
        normalize_content_granularity,
        normalize_evidence_kind,
        normalize_evidence_level,
        normalize_epistemic_status,
        normalize_fact_eligibility,
        normalize_materialization_state,
        normalize_status,
        parse_datetime,
        stable_id,
        timezone_or_default,
    )
except ImportError:  # direct test/import from the plugin directory
    from bot_personal_contract import BOT_PERSONAL_CANONICAL_SCHEMA_VERSION
    from agenda_contracts import (
        ACTOR_TYPES,
        AGENDA_STATUSES,
        AUTHORITY_KINDS,
        COMMITMENT_LEVELS,
        CONTENT_GRANULARITIES,
        EPISTEMIC_STATUSES,
        EVIDENCE_KINDS,
        FACT_ELIGIBILITIES,
        MATERIALIZATION_STATES,
        TEMPORAL_PHASES,
        derive_temporal_phase,
        normalize_authority_kind,
        normalize_commitment_level,
        normalize_content_granularity,
        normalize_evidence_kind,
        normalize_evidence_level,
        normalize_epistemic_status,
        normalize_fact_eligibility,
        normalize_materialization_state,
        normalize_status,
        parse_datetime,
        stable_id,
        timezone_or_default,
    )


DISCLOSURE_PURPOSES = frozenset(
    {
        "current_fact",
        "history_fact",
        "future_schedule",
        "schedule_commitment",
        "proactive",
        "memory_write",
        "diagnostic",
    }
)

# This is deliberately data, not a second lifecycle state machine.  The
# contract values are interpreted below and remain the single source of truth.
DISCLOSURE_PURPOSE_MATRIX: dict[str, dict[str, Any]] = {
    "current_fact": {
        "eligibilities": {"current_observed", "current_internal"},
        "description": "evidence-backed current activity or a short-lived Bot internal state",
    },
    "history_fact": {
        "eligibilities": {"history_observed"},
        "description": "evidence-backed completed or observed history",
    },
    "future_schedule": {
        "eligibilities": {"schedule_commitment"},
        "description": "future commitments and explicitly labelled soft plans",
    },
    "schedule_commitment": {
        "eligibilities": {"schedule_commitment"},
        "description": "valid external schedule commitment only",
    },
    "proactive": {
        "eligibilities": {"schedule_commitment", "current_internal"},
        "description": "near-term, minimum-necessary proactive context",
    },
    "memory_write": {
        "eligibilities": {"current_observed", "history_observed", "schedule_commitment"},
        "description": "structured hard facts and TTL-bound plans",
    },
    "diagnostic": {
        "eligibilities": set(FACT_ELIGIBILITIES),
        "description": "all candidates plus filtering decisions",
    },
}

# Short alias used by callers that refer to the matrix as ``USE_MATRIX``.
USE_MATRIX = DISCLOSURE_PURPOSE_MATRIX
DISCLOSURE_MATRIX = DISCLOSURE_PURPOSE_MATRIX

SELF_STATE_ALLOWED = frozenset({"在休息", "陪你聊天", "准备出门", "切到学习状态"})


@dataclass
class DisclosureView(Mapping[str, Any]):
    """Stable, JSON-compatible result of :meth:`AgendaDisclosurePolicy.build_view`."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    redactions: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    decision_trace: list[dict[str, Any]] = field(default_factory=list)
    purpose: str = "diagnostic"
    generated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": deepcopy(self.entries),
            "redactions": deepcopy(self.redactions),
            "source_refs": list(self.source_refs),
            "decision_trace": deepcopy(self.decision_trace),
            "purpose": self.purpose,
            "generated_at": self.generated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.as_dict()

    # Mapping compatibility makes the view convenient for old callers that
    # expected a dictionary while exposing attributes for new code.
    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("entries", "redactions", "source_refs", "decision_trace", "purpose", "generated_at"))

    def __len__(self) -> int:
        return 6

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)


class AgendaDisclosurePolicy:
    """Apply actor, time, evidence and purpose gates to agenda candidates."""

    PURPOSES = DISCLOSURE_PURPOSES
    PURPOSE_MATRIX = DISCLOSURE_PURPOSE_MATRIX
    USE_MATRIX = DISCLOSURE_PURPOSE_MATRIX
    # Soft proactive content should be close to now; hard commitments can be
    # surfaced across the configured future horizon.
    proactive_soft_horizon = timedelta(hours=2)
    proactive_hard_horizon = timedelta(hours=24)

    def __init__(
        self,
        *,
        bot_id: str = "",
        target_user_id: str = "",
        timezone_name: str = "Asia/Shanghai",
        proactive_horizon: timedelta | None = None,
        schedule_authority: Any = None,
    ) -> None:
        self.bot_id = str(bot_id or "").strip()
        self.target_user_id = str(target_user_id or "").strip()
        self.timezone_name = str(timezone_name or "Asia/Shanghai")
        self.schedule_authority = schedule_authority if hasattr(schedule_authority, "verify") else None
        if proactive_horizon is not None:
            self.proactive_hard_horizon = proactive_horizon

    def build_view(
        self,
        agenda: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
        now: datetime | None,
        purpose: str,
        target_user_id: str | None = None,
        max_entries: int | None = None,
    ) -> DisclosureView:
        purpose_text = str(purpose or "").strip().lower()
        if purpose_text not in DISCLOSURE_PURPOSES:
            raise ValueError(f"unknown agenda disclosure purpose: {purpose!r}")
        current = self._coerce_now(now)
        context = agenda if isinstance(agenda, Mapping) else {}
        bot_id = self.bot_id or str(context.get("bot_id") or "").strip()
        effective_target_user_id = (
            str(target_user_id).strip()
            if target_user_id is not None
            else self.target_user_id or str(context.get("target_user_id") or "").strip()
        )
        candidates = self._collect_candidates(agenda)
        # Infer a Bot ID only from an explicitly typed Bot subject.  Never use
        # target_user_id as the agenda owner.
        if not bot_id:
            for item in candidates:
                if str(item.get("actor_type") or "").strip().lower() == "bot" and item.get("subject_actor_id"):
                    bot_id = str(item.get("subject_actor_id"))
                    break

        canonical_candidates = [
            self._canonical_candidate(raw, current, index=index)
            for index, raw in enumerate(candidates)
        ]
        self._mark_stale_schedule_revisions(canonical_candidates, current)

        entries: list[dict[str, Any]] = []
        redactions: list[dict[str, Any]] = []
        source_refs: list[str] = []
        trace: list[dict[str, Any]] = []
        for candidate in canonical_candidates:
            candidate["_bot_id"] = bot_id
            candidate["_target_user_id"] = effective_target_user_id
            allowed, eligibility, reasons = self._decide(candidate, purpose_text, current)
            candidate["fact_eligibility"] = eligibility
            decision = {
                "entry_id": candidate["entry_id"],
                "purpose": purpose_text,
                "temporal_phase": candidate["temporal_phase"],
                "status": candidate["status"],
                "evidence_kind": candidate["evidence_kind"],
                "fact_eligibility": eligibility,
                "allowed": bool(allowed),
                "reasons": list(reasons),
            }
            trace.append(decision)
            if allowed:
                public = self._public_entry(candidate, purpose_text, eligibility)
                entries.append(public)
                for ref in public.get("source_refs") or []:
                    if ref not in source_refs:
                        source_refs.append(ref)
            else:
                redactions.append(
                    {
                        "entry_id": candidate["entry_id"],
                        "purpose": purpose_text,
                        "reason": reasons[0] if reasons else "purpose_filtered",
                        "reasons": list(reasons) if reasons else ["purpose_filtered"],
                    }
                )

            # Existing normalizer decisions are useful in diagnostics and in
            # the audit trail, but never flow into the public entries.
            for item_trace in candidate.get("decision_trace") or []:
                trace.append(
                    {
                        "entry_id": candidate["entry_id"],
                        "purpose": purpose_text,
                        "allowed": bool(allowed),
                        "stage": "normalizer",
                        **deepcopy(item_trace),
                    }
                )

        if purpose_text == "diagnostic":
            # Diagnostic consumers need all source references, including ones
            # rejected for ordinary disclosure, to explain a downgrade.
            source_refs = []
            for raw in candidates:
                for ref in self._refs(raw.get("source_refs")):
                    if ref not in source_refs:
                        source_refs.append(ref)
            diagnostic_entries: list[dict[str, Any]] = []
            for i, raw in enumerate(candidates):
                diagnostic_candidate = canonical_candidates[i]
                diagnostic_candidate["_bot_id"] = bot_id
                diagnostic_candidate["_target_user_id"] = effective_target_user_id
                _diag_allowed, diag_eligibility, diag_reasons = self._decide(diagnostic_candidate, purpose_text, current)
                diagnostic_candidate["fact_eligibility"] = diag_eligibility
                diagnostic_candidate["diagnostic_reasons"] = diag_reasons
                diagnostic_entries.append(self._diagnostic_entry(diagnostic_candidate))
            entries = diagnostic_entries
        if max_entries is not None:
            try:
                entries = entries[: max(0, int(max_entries))]
            except (TypeError, ValueError):
                pass
        return DisclosureView(
            entries=entries,
            redactions=redactions,
            source_refs=source_refs,
            decision_trace=trace,
            purpose=purpose_text,
            generated_at=current.isoformat(timespec="seconds"),
        )

    def _coerce_now(self, now: datetime | None) -> datetime:
        value = now or datetime.now().astimezone()
        try:
            return parse_datetime(value, timezone_name=self.timezone_name)
        except Exception:
            return value if value.tzinfo else value.replace(tzinfo=timezone_or_default(self.timezone_name))

    @staticmethod
    def _refs(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return []
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()[:200]
            if text and text not in result:
                result.append(text)
            if len(result) >= 50:
                break
        return result

    def _collect_candidates(self, agenda: Any) -> list[dict[str, Any]]:
        if isinstance(agenda, (list, tuple)):
            return [deepcopy(item) for item in agenda if isinstance(item, Mapping)]
        if not isinstance(agenda, Mapping):
            return []
        ordered: list[dict[str, Any]] = []
        entries = agenda.get("entries")
        if isinstance(entries, list) and entries:
            ordered.extend(deepcopy(item) for item in entries if isinstance(item, Mapping))
        else:
            for key in ("plans", "planned", "activities", "observed", "reconciliations", "reconciled"):
                value = agenda.get(key)
                if isinstance(value, list):
                    ordered.extend(deepcopy(item) for item in value if isinstance(item, Mapping))
            if isinstance(agenda.get("current"), Mapping):
                ordered.append(deepcopy(agenda["current"]))
            if not ordered and any(agenda.get(key) not in (None, "") for key in ("title", "activity", "summary", "plan_id", "activity_id", "event_id")):
                ordered.append(deepcopy(dict(agenda)))
        # Keep same-ID plan and activity records separate, but avoid exact
        # duplicates produced by nested C3 views.
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in ordered:
            ident = str(item.get("entry_id") or item.get("plan_id") or item.get("activity_id") or item.get("event_id") or "").strip()
            kind = str(item.get("kind") or item.get("source_kind") or "").strip().lower()
            key = (ident, kind) if ident else (stable_id("disclosure", item.get("title") or item.get("activity"), item.get("start_at") or item.get("start")), kind)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _revision_key(value: Any) -> tuple[Any, ...]:
        import re

        text = str(value or "").strip()[:80]
        parts: list[tuple[int, Any]] = []
        for part in re.split(r"([0-9]+)", text):
            if part.isdigit():
                parts.append((1, int(part)))
            elif part:
                parts.append((0, part.lower()))
        return tuple(parts)

    def _verify_schedule_ref(
        self,
        candidate: Mapping[str, Any],
        refs: list[str],
        now: datetime,
    ) -> tuple[str, str, bool]:
        """Verify the structured authority binding without trusting labels."""
        status, reason = validate_structured_schedule_ref(
            candidate.get("schedule_ref"),
            source_refs=refs,
            expected_authority=candidate.get("authority_kind"),
            expected_subject=candidate.get("subject_actor_id"),
            expected_target=candidate.get("target_user_id"),
            now=now,
            adapter=self.schedule_authority,
        )
        return status, reason, status != "invalid"

    def _mark_stale_schedule_revisions(self, candidates: list[dict[str, Any]], now: datetime) -> None:
        """Keep only the latest structurally valid source revision per event."""

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for candidate in candidates:
            if not candidate.get("_schedule_ref_structurally_valid"):
                continue
            ref = candidate.get("schedule_ref")
            if not isinstance(ref, Mapping):
                continue
            key = (str(ref.get("namespace") or ""), str(ref.get("event_id") or ""))
            if key != ("", ""):
                groups.setdefault(key, []).append(candidate)
        for group in groups.values():
            if len(group) < 2:
                continue

            def rank(item: Mapping[str, Any]) -> tuple[Any, ...]:
                ref = item.get("schedule_ref") if isinstance(item.get("schedule_ref"), Mapping) else {}
                updated = str(ref.get("updated_at") or "")
                try:
                    updated_key: Any = parse_datetime(updated, timezone_name=self.timezone_name)
                except Exception:
                    updated_key = datetime.min.replace(tzinfo=now.tzinfo)
                # Revision is the provider's explicit ordering; updated_at
                # breaks ties for providers that reuse opaque revision IDs.
                return (self._revision_key(ref.get("revision")), updated_key)

            winner = max(group, key=rank)
            for candidate in group:
                if candidate is not winner:
                    candidate["_schedule_revision_superseded"] = True
                    candidate["_schedule_ref_reason"] = "schedule_revision_superseded"

    def _canonical_candidate(self, raw: Mapping[str, Any], now: datetime, *, index: int = 0) -> dict[str, Any]:
        candidate = deepcopy(dict(raw))
        title = str(candidate.get("title") or candidate.get("activity") or candidate.get("summary") or candidate.get("description") or "").strip()[:240]
        candidate["title"] = title
        candidate["entry_id"] = str(candidate.get("entry_id") or candidate.get("plan_id") or candidate.get("activity_id") or candidate.get("event_id") or stable_id("disclosure", title, candidate.get("start_at") or candidate.get("start"), index))
        source_kind = str(candidate.get("source_kind") or "").strip().lower()
        if source_kind not in {"planned", "observed", "projection", "reconciled"}:
            source_kind = "observed" if str(candidate.get("kind") or "").lower() == "observed" else "planned"
        candidate["source_kind"] = source_kind
        legacy_status = candidate.get("status") if candidate.get("status") not in (None, "") else candidate.get("lifecycle_status")
        status = normalize_status(legacy_status, "active" if source_kind == "observed" else "planned")
        evidence_kind = normalize_evidence_kind(candidate.get("evidence_kind"), "")
        source_text = str(candidate.get("source") or "").strip().lower()
        kind_text = str(candidate.get("kind") or "").strip().lower()
        if not evidence_kind:
            if source_kind == "planned":
                evidence_kind = "none"
            elif source_text in {"tool", "tool_action", "api", "browser"} or kind_text in {"tool", "tool_action"}:
                evidence_kind = "tool_action"
            elif source_text in {"self_state", "self_state_commit", "runtime"}:
                evidence_kind = "self_state_commit"
            elif source_text in {"conversation", "chat", "interaction", "message"} or kind_text in {"conversation", "chat", "interaction"}:
                evidence_kind = "interaction"
            else:
                evidence_kind = "external_record" if source_kind == "observed" else "none"
        candidate["evidence_kind"] = evidence_kind
        # Statuses on ordinary plans are never evidence; leave reconciler
        # results visible only when an explicit compatible evidence kind exists.
        if source_kind == "planned" and status != "planned" and evidence_kind == "none":
            candidate["legacy_status"] = legacy_status
            status = "unknown"
        candidate["status"] = status
        refs = self._refs(candidate.get("source_refs"))
        candidate["source_refs"] = refs
        candidate["runtime_origin_refs"] = self._refs(candidate.get("runtime_origin_refs"))
        # A reference is never trusted merely because it is non-empty.  Hard
        # schedule authorities must carry a complete adapter-issued
        # ``schedule_ref`` whose stable ref_id is present in source_refs.
        trust_keys = ("source_refs_trusted", "trusted_source_refs", "authority_verified")
        explicit_trust = any(bool(candidate.get(key)) for key in trust_keys)
        explicit_denied = any(
            key in candidate
            and str(candidate.get(key)).strip().lower() in {"false", "no", "denied", "invalid", "revoked"}
            for key in trust_keys
        )
        candidate["source_refs_trusted"] = explicit_trust
        authority = normalize_authority_kind(candidate.get("authority_kind"), "llm" if source_kind == "planned" else "state")
        candidate["authority_kind"] = authority
        commitment = normalize_commitment_level(candidate.get("commitment_level"), "tentative")
        if authority == "routine":
            commitment = "routine"
        candidate["_schedule_ref_status"] = "not_applicable"
        candidate["_schedule_ref_reason"] = ""
        candidate["_schedule_ref_structurally_valid"] = False
        if authority in {"calendar", "timetable", "roster", "appointment", "user_confirmation"}:
            schedule_status, schedule_reason, schedule_structural = self._verify_schedule_ref(candidate, refs, now)
            candidate["_schedule_ref_status"] = schedule_status
            candidate["_schedule_ref_reason"] = schedule_reason
            candidate["_schedule_ref_structurally_valid"] = schedule_structural
            candidate["source_refs_trusted"] = bool(schedule_status == "valid" and not explicit_denied)
            if schedule_structural and isinstance(candidate.get("schedule_ref"), Mapping):
                # Disclosure timing comes from the signed reference, not from
                # caller-controlled duplicate clock fields.
                candidate["start_at"] = candidate["schedule_ref"].get("effective_from")
                candidate["end_at"] = candidate["schedule_ref"].get("effective_to")
        if authority in {"calendar", "timetable", "roster", "appointment", "user_confirmation"} and refs and candidate["source_refs_trusted"]:
            commitment = "confirmed"
        elif authority in {"calendar", "timetable", "roster", "appointment", "user_confirmation"} and commitment == "confirmed":
            commitment = "tentative"
        elif authority not in {"calendar", "timetable", "roster", "appointment", "user_confirmation"} and commitment == "confirmed":
            commitment = "tentative"
        candidate["commitment_level"] = commitment
        phase_item = candidate
        if evidence_kind == "self_state_commit" and not any(candidate.get(key) for key in ("start_at", "start", "time")):
            phase_item = dict(candidate)
            phase_item["start_at"] = candidate.get("committed_at") or candidate.get("created_at")
            phase_item["end_at"] = candidate.get("valid_until") or candidate.get("expires_at")
        candidate["temporal_phase"] = derive_temporal_phase(phase_item, now=now, timezone_name=self.timezone_name)
        candidate["evidence_level"] = normalize_evidence_level(candidate.get("evidence_level"), "L2" if source_kind == "observed" else "L0")
        candidate["canonical_evidence_level"] = normalize_evidence_level(candidate.get("canonical_evidence_level") or candidate["evidence_level"], candidate["evidence_level"])
        archive_default = candidate["canonical_evidence_level"] if candidate["canonical_evidence_level"] in {"L0", "L1", "L2", "L3"} else "L3"
        candidate["archive_evidence_level"] = normalize_evidence_level(candidate.get("archive_evidence_level"), archive_default)
        candidate["evidence_level_mapping"] = {
            "canonical_evidence_level": candidate["canonical_evidence_level"],
            "archive_evidence_level": candidate["archive_evidence_level"],
            "lossy": candidate["canonical_evidence_level"] != candidate["archive_evidence_level"],
        }
        candidate["authority_kind"] = authority
        candidate["epistemic_status"] = normalize_epistemic_status(candidate.get("epistemic_status"), "observed" if source_kind == "observed" else "inferred")
        candidate["content_granularity"] = normalize_content_granularity(candidate.get("content_granularity"), "intent")
        candidate["materialization_state"] = normalize_materialization_state(candidate.get("materialization_state"), "active" if source_kind == "observed" else "none")
        candidate["fact_eligibility"] = "none"
        try:
            candidate["confidence"] = max(0.0, min(1.0, float(candidate.get("confidence", 0.75 if source_kind == "observed" else 0.4))))
        except (TypeError, ValueError):
            candidate["confidence"] = 0.75 if source_kind == "observed" else 0.4
        candidate["actor_type"] = str(candidate.get("actor_type") or "").strip().lower()
        if candidate["actor_type"] not in ACTOR_TYPES:
            candidate["actor_type"] = ""
        candidate["subject_actor_id"] = str(candidate.get("subject_actor_id") or "").strip()
        candidate["object_actor_id"] = str(candidate.get("object_actor_id") or "").strip()
        candidate["source_actor_id"] = str(candidate.get("source_actor_id") or "system").strip()
        candidate["target_user_id"] = str(candidate.get("target_user_id") or "").strip()
        candidate["participant_roles"] = deepcopy(candidate.get("participant_roles") if isinstance(candidate.get("participant_roles"), list) else candidate.get("participants") or [])
        candidate["decision_trace"] = deepcopy(candidate.get("decision_trace") if isinstance(candidate.get("decision_trace"), list) else [])
        try:
            candidate["canonical_schema_version"] = max(
                1, int(candidate.get("canonical_schema_version") or BOT_PERSONAL_CANONICAL_SCHEMA_VERSION)
            )
        except (TypeError, ValueError):
            candidate["canonical_schema_version"] = BOT_PERSONAL_CANONICAL_SCHEMA_VERSION
        return candidate

    def _actor_reasons(self, candidate: Mapping[str, Any]) -> list[str]:
        subject = str(candidate.get("subject_actor_id") or "").strip()
        actor_type = str(candidate.get("actor_type") or "").strip().lower()
        bot_id = str(candidate.get("_bot_id") or "").strip()
        target = str(candidate.get("_target_user_id") or "").strip()
        candidate_target = str(candidate.get("target_user_id") or "").strip()
        schedule_ref = candidate.get("schedule_ref")
        schedule_target = (
            str(schedule_ref.get("target_user_id") or "").strip()
            if isinstance(schedule_ref, Mapping)
            else ""
        )
        if target and candidate_target and candidate_target != target:
            return ["schedule_target_mismatch"]
        if target and schedule_target and schedule_target != target:
            return ["schedule_target_mismatch"]
        if candidate_target and schedule_target and candidate_target != schedule_target:
            return ["schedule_target_mismatch"]
        if not subject:
            return ["missing_subject"]
        if actor_type == "bot":
            if bot_id and subject != bot_id:
                return ["subject_mismatch_bot"]
            return []
        if actor_type == "interlocutor_user":
            if target and subject == target:
                return []
            return ["subject_mismatch_user"]
        # External/system statements cannot become a Bot/User execution fact.
        return ["subject_scope_external"]

    @staticmethod
    def _interaction_is_scoped(candidate: Mapping[str, Any]) -> bool:
        title = " ".join(str(candidate.get(key) or "").lower() for key in ("title", "activity", "summary"))
        interaction_tokens = ("chat", "conversation", "interaction", "message", "聊天", "对话", "互动", "陪伴")
        # When a concrete title exists, it must itself describe the
        # interaction.  A generic ``source=conversation`` label cannot make
        # an unrelated title such as "attend class" an execution fact.
        if title.strip():
            return any(token in title for token in interaction_tokens)
        haystack = " ".join(str(candidate.get(key) or "").lower() for key in ("kind", "source"))
        return any(token in haystack for token in interaction_tokens)

    def _expiry_reason(self, candidate: Mapping[str, Any], now: datetime) -> str | None:
        for key in ("expires_at", "valid_until", "effective_to"):
            value = candidate.get(key)
            if value in (None, ""):
                continue
            try:
                if parse_datetime(value, timezone_name=self.timezone_name) <= now:
                    return "expired"
            except Exception:
                return "invalid_expiry"
        schedule_ref = candidate.get("schedule_ref")
        if isinstance(schedule_ref, Mapping) and schedule_ref.get("expires_at"):
            try:
                if parse_datetime(schedule_ref.get("expires_at"), timezone_name=self.timezone_name) <= now:
                    return "expired"
            except Exception:
                return "invalid_expiry"
        return None

    def _self_state_reasons(self, candidate: Mapping[str, Any], now: datetime) -> list[str]:
        reasons: list[str] = []
        if str(candidate.get("actor_type") or "").strip().lower() != "bot":
            reasons.append("self_state_subject_not_bot")
        if str(candidate.get("state") or candidate.get("title") or "").strip() not in SELF_STATE_ALLOWED:
            reasons.append("self_state_state_not_allowed")
        if AgendaDisclosurePolicy._refs(candidate.get("source_refs")):
            reasons.append("self_state_source_refs_forbidden")
        if not AgendaDisclosurePolicy._refs(candidate.get("runtime_origin_refs")):
            reasons.append("self_state_origin_missing")
        committed_raw = candidate.get("committed_at") or candidate.get("start_at") or candidate.get("start")
        until_raw = candidate.get("valid_until") or candidate.get("end_at") or candidate.get("expires_at")
        try:
            committed = parse_datetime(committed_raw, timezone_name=self.timezone_name)
            until = parse_datetime(until_raw, timezone_name=self.timezone_name)
            if committed > now:
                reasons.append("self_state_not_started")
            if until <= now:
                reasons.append("expired")
            if until <= committed:
                reasons.append("self_state_interval_invalid")
        except Exception:
            reasons.append("self_state_time_invalid")
        # A self-state is deliberately a broad internal intent.  Reject
        # obvious external-result claims even if a caller supplied the right
        # evidence_kind label.
        text = " ".join(str(candidate.get(key) or "") for key in ("state", "title", "activity", "summary")).lower()
        forbidden = (
            "付款",
            "支付",
            "发布",
            "定位",
            "签到",
            "打卡",
            "到场",
            "通话",
            "上课",
            "上班",
            "arrived",
            "check-in",
            "checked in",
            "paid",
            "publish",
            "payment",
            "location",
            "call",
        )
        if any(token in text for token in forbidden):
            reasons.append("self_state_external_result_forbidden")
        return reasons

    def _base_eligibility(self, candidate: Mapping[str, Any], now: datetime) -> tuple[str, list[str]]:
        reasons: list[str] = []
        reasons.extend(self._actor_reasons(candidate))
        status = str(candidate.get("status") or "unknown")
        phase = str(candidate.get("temporal_phase") or "future")
        source_kind = str(candidate.get("source_kind") or "planned")
        evidence_kind = str(candidate.get("evidence_kind") or "none")
        refs = self._refs(candidate.get("source_refs"))
        refs_trusted = bool(candidate.get("source_refs_trusted"))
        expiry = self._expiry_reason(candidate, now)
        if expiry:
            reasons.append(expiry)
        schedule_ref = candidate.get("schedule_ref")
        source_state = candidate.get("source_state") or candidate.get("ref_state") or candidate.get("state")
        if isinstance(schedule_ref, Mapping):
            source_state = source_state or schedule_ref.get("state")
        schedule_status = str(candidate.get("_schedule_ref_status") or "").lower()
        schedule_reason = str(candidate.get("_schedule_ref_reason") or "").strip()
        if schedule_status == "expired":
            reasons.append("expired")
        elif schedule_status in {"cancelled", "revoked"}:
            reasons.append("status_cancelled")
        elif schedule_status == "invalid" and candidate.get("authority_kind") in {
            "calendar",
            "timetable",
            "roster",
            "appointment",
            "user_confirmation",
        }:
            reasons.append(schedule_reason or "invalid_schedule_ref")
        if candidate.get("_schedule_revision_superseded"):
            reasons.append("schedule_revision_superseded")
        if str(source_state or "").strip().lower() in {"cancelled", "canceled", "revoked", "superseded"}:
            reasons.append("status_cancelled")
        if status in {"cancelled", "overridden", "deferred"}:
            reasons.append(f"status_{status}")
        if candidate.get("materialization_state") == "rejected":
            reasons.append("materialization_rejected")

        # Hard/soft schedule intent.  This is deliberately separate from
        # execution eligibility: an untrusted soft plan may be summarized as a
        # tentative future possibility but never as a current/history fact.
        if source_kind == "projection":
            reasons.append("projection_not_fact")
        if source_kind == "planned" and phase in {"future", "current"} and status == "planned":
            commitment = str(candidate.get("commitment_level") or "tentative")
            if commitment in COMMITMENT_LEVELS and candidate.get("content_granularity") != "scene":
                if commitment == "confirmed" and not (refs and refs_trusted):
                    reasons.append("untrusted_schedule_source")
                return "schedule_commitment", reasons

        if candidate.get("materialization_state") == "candidate" and evidence_kind in {
            "interaction",
            "self_state_commit",
            "tool_action",
            "external_record",
        }:
            reasons.append("candidate_not_fact")
            return "none", reasons

        if evidence_kind == "interaction":
            if not self._interaction_is_scoped(candidate):
                reasons.append("interaction_scope_mismatch")
                return "none", reasons
            if phase == "current" and status in {"active", "partially_completed"} and refs:
                return "current_observed", reasons
            if phase == "past" and status in {"completed", "partially_completed", "active"} and refs:
                # Conversation evidence can establish that the Bot was
                # chatting with the current user earlier, but it cannot prove
                # an unrelated external action.
                return "history_observed", reasons
            reasons.append("interaction_not_current")
            return "none", reasons
        if evidence_kind == "self_state_commit":
            reasons.extend(self._self_state_reasons(candidate, now))
            if (
                not reasons
                and phase == "current"
                and status in {"active", "planned"}
                and candidate.get("materialization_state") == "active"
            ):
                return "current_internal", reasons
            if not reasons:
                reasons.append("self_state_not_current")
            return "none", reasons
        if evidence_kind in {"tool_action", "external_record"}:
            if not refs:
                reasons.append("missing_evidence_refs")
                return "none", reasons
            if not refs_trusted:
                reasons.append("untrusted_evidence_refs")
                return "none", reasons
            if phase == "current" and status in {"active", "partially_completed"}:
                return "current_observed", reasons
            if phase == "past" and status in {"completed", "partially_completed", "active"}:
                return "history_observed", reasons
            reasons.append("evidence_time_mismatch")
            return "none", reasons
        if evidence_kind == "external_commitment":
            if not (refs and refs_trusted):
                reasons.append("untrusted_schedule_source")
                return "none", reasons
            if candidate.get("commitment_level") in COMMITMENT_LEVELS and phase in {"future", "current"}:
                return "schedule_commitment", reasons
        if status in {"completed", "active", "partially_completed"}:
            reasons.append("missing_execution_evidence")
        return "none", reasons

    def _decide(self, candidate: dict[str, Any], purpose: str, now: datetime) -> tuple[bool, str, list[str]]:
        eligibility, reasons = self._base_eligibility(candidate, now)
        phase = candidate["temporal_phase"]
        status = candidate["status"]
        commitment = candidate["commitment_level"]
        # Diagnostics intentionally retain rejected/missing-subject candidates.
        if purpose == "diagnostic":
            return True, eligibility, reasons
        if reasons and any(
            reason
            in {
                "missing_subject",
                "subject_mismatch_bot",
                "subject_mismatch_user",
                "subject_scope_external",
                "expired",
                "invalid_expiry",
                "status_cancelled",
                "status_revoked",
                "status_overridden",
                "status_deferred",
                "materialization_rejected",
                "invalid_schedule_ref",
                "permission_denied",
                "schedule_ref_not_in_source_refs",
                "schedule_ref_incomplete",
                "schedule_authority_mismatch",
                "schedule_subject_mismatch",
                "schedule_target_mismatch",
                "schedule_revision_superseded",
            }
            for reason in reasons
        ):
            return False, "none", reasons
        if purpose == "current_fact":
            if eligibility in {"current_observed", "current_internal"} and phase == "current":
                return True, eligibility, reasons
            if eligibility == "schedule_commitment":
                reasons = [*reasons, "planned_without_execution_evidence"]
            return False, "none", reasons or ["future_plan_not_current"]
        if purpose == "history_fact":
            if eligibility == "history_observed" and phase == "past":
                return True, eligibility, reasons
            if candidate.get("source_kind") in {"planned", "projection", "reconciled"}:
                reasons = [*reasons, "planned_without_execution_evidence"]
            return False, "none", reasons or ["no_history_evidence"]
        if purpose == "future_schedule":
            if eligibility == "schedule_commitment" and phase in {"future", "current"} and commitment in COMMITMENT_LEVELS:
                return True, eligibility, reasons
            return False, "none", reasons or ["not_future_schedule"]
        if purpose == "schedule_commitment":
            hard_authority = candidate["authority_kind"] in {"calendar", "timetable", "roster", "appointment", "user_confirmation"}
            if eligibility == "schedule_commitment" and hard_authority and candidate.get("source_refs") and candidate.get("source_refs_trusted") and status not in {"cancelled", "overridden", "deferred"}:
                return True, eligibility, reasons
            return False, "none", reasons or ["missing_trusted_schedule_source"]
        if purpose == "proactive":
            if eligibility == "current_internal" and phase == "current":
                return True, eligibility, reasons
            start_value = candidate.get("start_at") or candidate.get("start") or candidate.get("time")
            try:
                start = parse_datetime(start_value, timezone_name=self.timezone_name)
            except Exception:
                start = now
            delta = start - now
            horizon = self.proactive_soft_horizon if commitment == "tentative" else self.proactive_hard_horizon
            if eligibility == "schedule_commitment" and phase == "future" and timedelta(0) <= delta <= horizon:
                if candidate.get("content_granularity") != "scene":
                    return True, eligibility, reasons
            return False, "none", reasons or ["not_proactive_window"]
        if purpose == "memory_write":
            if eligibility == "history_observed" and phase == "past":
                return True, eligibility, reasons
            if eligibility == "current_observed" and phase == "current":
                return True, eligibility, reasons
            if eligibility == "schedule_commitment" and phase in {"future", "current"}:
                # Soft plans require an explicit TTL.  A validated hard
                # schedule has an adapter-owned absolute end time and may be
                # archived as a commitment without becoming an execution
                # history fact.
                if candidate.get("expires_at"):
                    return True, eligibility, reasons
                hard_authority = candidate.get("authority_kind") in {
                    "calendar",
                    "timetable",
                    "roster",
                    "appointment",
                    "user_confirmation",
                }
                schedule_ref = candidate.get("schedule_ref")
                if hard_authority and candidate.get("source_refs_trusted") and isinstance(schedule_ref, Mapping) and schedule_ref.get("effective_to"):
                    return True, eligibility, reasons
            return False, "none", reasons or ["memory_requires_evidence_or_ttl"]
        return False, "none", ["purpose_filtered"]

    @staticmethod
    def _public_entry(candidate: Mapping[str, Any], purpose: str, eligibility: str) -> dict[str, Any]:
        result = deepcopy(dict(candidate))
        for key in (
            "_bot_id",
            "_target_user_id",
            "decision_trace",
            "diagnostic_reasons",
            "basis",
            "scene_details",
            "candidate_options",
            "runtime_origin_refs",
        ):
            result.pop(key, None)
        result["fact_eligibility"] = eligibility
        result["disclosure_purpose"] = purpose
        # Untrusted refs are useful in diagnostics but cannot be presented as
        # evidence to a downstream language model.
        if not candidate.get("source_refs_trusted"):
            result["source_refs"] = []
        if purpose in {"future_schedule", "proactive"} and result.get("content_granularity") == "scene":
            result["content_granularity"] = "candidate"
            result["materialization_state"] = "candidate"
            result["title"] = str(result.get("title") or "").strip()[:120]
        return result

    @staticmethod
    def _diagnostic_entry(candidate: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(candidate))
        result.pop("_bot_id", None)
        result.pop("_target_user_id", None)
        return result


def build_view(
    agenda: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    now: datetime | None,
    purpose: str,
    **policy_kwargs: Any,
) -> DisclosureView:
    """Small functional facade for callers that do not need a policy object."""

    target_user_id = policy_kwargs.pop("target_user_id", None)
    max_entries = policy_kwargs.pop("max_entries", None)
    return AgendaDisclosurePolicy(**policy_kwargs).build_view(
        agenda,
        now,
        purpose,
        target_user_id=target_user_id,
        max_entries=max_entries,
    )


__all__ = [
    "AgendaDisclosurePolicy",
    "DisclosureView",
    "DISCLOSURE_PURPOSES",
    "DISCLOSURE_PURPOSE_MATRIX",
    "USE_MATRIX",
    "DISCLOSURE_MATRIX",
    "build_view",
]
