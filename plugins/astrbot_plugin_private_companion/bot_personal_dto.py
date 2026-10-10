# -*- coding: utf-8 -*-
"""Privacy-limited DTOs for the Bot Personal archive boundary."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Any

from .bot_personal_contract import (
    BOT_PERSONAL_CANONICAL_SCHEMA_VERSION,
    BOT_PERSONAL_LEGACY_CANONICAL_SCHEMA_VERSIONS,
    BOT_PERSONAL_MEMORY_DOMAIN,
    BOT_PERSONAL_MEMORY_TYPES,
    BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION,
    BOT_PERSONAL_SUBJECT,
    TYPE_CONTRACTS,
    normalize_window,
    window_for_minutes,
)

try:
    from .schedule_authority import validate_structured_schedule_ref
except ImportError:
    from schedule_authority import validate_structured_schedule_ref


FORBIDDEN_KEY_PARTS = {
    "prompt", "conversation", "chat_history", "transcript", "message_chain", "contexts",
    "cookie", "token", "password", "passwd", "secret", "credential", "authorization",
    "api_key", "apikey", "access_key", "private_key", "raw_message", "binary",
    "media_bytes", "media_binary", "media_data", "media_content", "media_blob",
    "image_bytes", "audio_bytes", "video_bytes", "base64",
}
CERTAINTY_ALIASES = {"high": 0.9, "medium": 0.6, "low": 0.3, "高": 0.9, "中": 0.6, "低": 0.3}


class PrivacyRejected(ValueError):
    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"privacy_rejected:{path}:{reason}")


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _certainty(value: Any, default: float = 0.6) -> float:
    if isinstance(value, str):
        value = CERTAINTY_ALIASES.get(value.strip().lower(), value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _parse_moment(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed


def _looks_sensitive_value(value: str) -> str:
    text = value.strip()
    lowered = text.lower()
    if re.search(r"(?:password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]", lowered):
        return "credential_pattern"
    if re.search(r"^bearer\s+", lowered):
        return "authorization_value"
    if "base64," in lowered or lowered.startswith("data:"):
        return "base64_or_data_uri"
    if "-----begin " in lowered:
        return "private_key_material"
    if re.search(r"(?:^|[\s=:;,])(?:[a-z]:[\\/]|\\\\|/home/|/root/|/tmp/|/var/|/volume\d+/)", text, re.IGNORECASE):
        return "absolute_path"
    return ""


def validate_bot_personal_key(value: Any, *, field: str = "idempotency_key") -> None:
    reason = _looks_sensitive_value(str(value or ""))
    if reason:
        raise PrivacyRejected(field, reason)
    if not _text(value, 240):
        raise PrivacyRejected(field, "missing")


def validate_bot_personal_payload(value: Any, *, path: str = "payload", depth: int = 0) -> None:
    if depth > 8:
        raise PrivacyRejected(path, "max_depth")
    if isinstance(value, dict):
        if len(value) > 64:
            raise PrivacyRejected(path, "too_many_fields")
        for key, item in value.items():
            name = _text(key, 80)
            lowered = name.lower().replace("-", "_")
            if not name or any(part in lowered for part in FORBIDDEN_KEY_PARTS):
                raise PrivacyRejected(f"{path}.{name}", "forbidden_key")
            validate_bot_personal_payload(item, path=f"{path}.{name}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise PrivacyRejected(path, "too_many_items")
        for index, item in enumerate(value):
            validate_bot_personal_payload(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PrivacyRejected(path, "binary_media")
    if isinstance(value, str):
        reason = _looks_sensitive_value(value)
        if reason:
            raise PrivacyRejected(path, reason)
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise PrivacyRejected(path, "unsupported_value")


def _safe_value(value: Any, *, depth: int = 0, max_depth: int = 8) -> Any:
    if depth > max_depth:
        return None
    if isinstance(value, dict):
        return {
            _text(key, 80): _safe_value(item, depth=depth + 1, max_depth=max_depth)
            for key, item in list(value.items())[:64]
            if _text(key, 80)
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1, max_depth=max_depth) for item in list(value)[:64]]
    if isinstance(value, str):
        return _text(value, 1200)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text(value, 240)


def _derive_date(value: Any, occurred_at: str, now: datetime) -> str:
    explicit = _text(value, 32)
    if explicit:
        return explicit.split("T", 1)[0]
    parsed = _parse_moment(occurred_at) or now.astimezone()
    return parsed.date().isoformat()


def _derive_window(value: Any, occurred_at: str, now: datetime) -> str:
    explicit = normalize_window(value)
    parsed = _parse_moment(occurred_at) or now.astimezone()
    if explicit:
        return explicit
    return window_for_minutes(parsed.hour * 60 + parsed.minute)


def _has_trusted_schedule_ref(payload: dict[str, Any], authority: str, now: datetime) -> bool:
    """Accept only a complete adapter-shaped reference, never a trust label."""
    try:
        status, _reason = validate_structured_schedule_ref(
            payload.get("schedule_ref"),
            source_refs=payload.get("source_refs"),
            expected_authority=authority,
            expected_subject=BOT_PERSONAL_SUBJECT,
            expected_target=payload.get("target_user_id"),
            now=now,
        )
        return status == "valid"
    except Exception:
        return False


@dataclass(frozen=True)
class BotPersonalArchiveDTO:
    record_id: str
    memory_domain: str
    memory_type: str
    subject: str
    date: str
    window: str
    window_date: str
    occurred_at: str
    created_at: str
    updated_at: str
    source_kind: str
    source_refs: list[str]
    certainty: float
    evidence_level: str
    status: str
    version: int
    idempotency_key: str
    payload_schema_version: str
    payload: dict[str, Any]
    # Canonical agenda axes are additive.  ``certainty`` above intentionally
    # remains the legacy numeric field for old memory consumers.
    evidence_kind: str = "none"
    canonical_evidence_level: str = "L0"
    archive_evidence_level: str = "L0"
    evidence_level_mapping: dict[str, Any] | None = None
    authority_kind: str = "llm"
    commitment_level: str = "tentative"
    epistemic_status: str = "inferred"
    content_granularity: str = "intent"
    materialization_state: str = "none"
    fact_eligibility: str = "none"
    actor_type: str = "bot"
    subject_actor_id: str = BOT_PERSONAL_SUBJECT
    object_actor_id: str = ""
    source_actor_id: str = "system"
    target_user_id: str = ""
    participant_roles: list[Any] | None = None
    runtime_origin_refs: list[str] | None = None
    expires_at: str = ""
    decision_trace: list[dict[str, Any]] | None = None
    owner_bot_id: str = ""
    persona_id: str = ""
    canonical_schema_version: int = BOT_PERSONAL_CANONICAL_SCHEMA_VERSION

    def envelope(self) -> dict[str, Any]:
        result = {
            "record_id": self.record_id,
            "memory_domain": self.memory_domain,
            "memory_type": self.memory_type,
            "subject": self.subject,
            "date": self.date,
            "window": self.window,
            "window_date": self.window_date,
            "occurred_at": self.occurred_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_kind": self.source_kind,
            "source_refs": list(self.source_refs),
            "certainty": self.certainty,
            "evidence_level": self.evidence_level,
            "status": self.status,
            "version": self.version,
            "idempotency_key": self.idempotency_key,
            "payload_schema_version": self.payload_schema_version,
            "payload": deepcopy(self.payload),
            "evidence_kind": self.evidence_kind,
            "canonical_evidence_level": self.canonical_evidence_level,
            "archive_evidence_level": self.archive_evidence_level,
            "evidence_level_mapping": deepcopy(self.evidence_level_mapping or {}),
            "authority_kind": self.authority_kind,
            "commitment_level": self.commitment_level,
            "epistemic_status": self.epistemic_status,
            "content_granularity": self.content_granularity,
            "materialization_state": self.materialization_state,
            "fact_eligibility": self.fact_eligibility,
            "actor_type": self.actor_type,
            "subject_actor_id": self.subject_actor_id,
            "object_actor_id": self.object_actor_id,
            "source_actor_id": self.source_actor_id,
            "target_user_id": self.target_user_id,
            "participant_roles": deepcopy(self.participant_roles or []),
            "runtime_origin_refs": list(self.runtime_origin_refs or []),
            "expires_at": self.expires_at,
            "decision_trace": deepcopy(self.decision_trace or []),
            "canonical_schema_version": self.canonical_schema_version,
        }
        if self.canonical_schema_version >= BOT_PERSONAL_CANONICAL_SCHEMA_VERSION:
            result["owner_bot_id"] = self.owner_bot_id
            result["persona_id"] = self.persona_id
        return result


def build_bot_personal_dto(
    *,
    memory_type: str,
    kind: str = "",
    namespace: str = "",
    payload: dict[str, Any],
    idempotency_key: str,
    occurred_at: str,
    now: datetime | None = None,
    version: int = 1,
    owner_bot_id: str = "",
    persona_id: str = "",
    canonical_schema_version: int | None = None,
) -> BotPersonalArchiveDTO:
    del kind, namespace
    if memory_type not in BOT_PERSONAL_MEMORY_TYPES:
        raise ValueError(f"invalid_memory_type:{memory_type}")
    validate_bot_personal_payload(payload)
    validate_bot_personal_key(idempotency_key)
    # Keep direct/legacy callers on canonical v2 until they explicitly
    # provide the v3 producer namespace.  The adapter passes v3 after bridge
    # capability negotiation.
    requested_canonical_schema = int(
        canonical_schema_version
        if canonical_schema_version is not None
        else max(BOT_PERSONAL_LEGACY_CANONICAL_SCHEMA_VERSIONS)
    )
    supported_canonical_schemas = {
        *BOT_PERSONAL_LEGACY_CANONICAL_SCHEMA_VERSIONS,
        BOT_PERSONAL_CANONICAL_SCHEMA_VERSION,
    }
    if requested_canonical_schema not in supported_canonical_schemas:
        raise ValueError("unsupported_canonical_schema")
    owner_bot_id = _text(owner_bot_id, 120)
    persona_id = _text(persona_id, 96)
    if requested_canonical_schema >= BOT_PERSONAL_CANONICAL_SCHEMA_VERSION:
        if not owner_bot_id or not persona_id:
            raise ValueError("invalid_namespace")
    else:
        owner_bot_id = ""
        persona_id = "legacy"
    current = now or datetime.now().astimezone()
    safe = _safe_value(payload) or {}
    subject_actor = _text(safe.get("subject_actor_id"), 120) or BOT_PERSONAL_SUBJECT
    if subject_actor != BOT_PERSONAL_SUBJECT:
        # This DTO is the Bot-owned archive boundary.  A user assertion or a
        # foreign actor must not be silently rewritten into Bot history.
        raise ValueError("subject_actor_id_mismatch")
    safe["subject_actor_id"] = BOT_PERSONAL_SUBJECT
    actor_type = _text(safe.get("actor_type"), 32) or "bot"
    if actor_type != "bot":
        raise ValueError("actor_type_mismatch")
    safe["actor_type"] = "bot"
    occurred = _text(occurred_at, 80) or current.isoformat(timespec="seconds")
    date_key = _derive_date(safe.get("date") or safe.get("window_date"), occurred, current)
    window = _derive_window(safe.get("window"), occurred, current)
    if not window:
        raise ValueError("invalid_window")
    source_refs: list[str] = []
    for item in safe.get("source_refs") or []:
        ref = _text(item, 240)
        if ref and ref not in source_refs:
            source_refs.append(ref)
    if not source_refs:
        source_refs = [f"archive:{_text(idempotency_key, 240)}"]
    contract = TYPE_CONTRACTS[memory_type]
    source_kind, default_evidence, default_status = contract
    # The archive envelope is also a write gate.  A plain schedule payload is
    # an intent regardless of model-provided lifecycle/evidence fields; only a
    # later C3 evidence adapter may create an observed/completed record.
    schedule_trusted = False
    if memory_type == "bot_schedule_plan":
        raw_status = _text(safe.get("status"), 32).lower()
        if raw_status and raw_status != "planned":
            safe["legacy_status"] = raw_status
        raw_source_kind = _text(safe.get("source_kind"), 32).lower()
        if raw_source_kind and raw_source_kind != "planned":
            safe["legacy_source_kind"] = raw_source_kind
        raw_refs = list(safe.get("source_refs") or []) if isinstance(safe.get("source_refs"), list) else []
        trusted_refs = _has_trusted_schedule_ref(safe, _text(safe.get("authority_kind"), 48), current)
        schedule_trusted = trusted_refs
        if raw_refs and not trusted_refs:
            safe["legacy_source_refs"] = raw_refs[:30]
            safe["source_refs"] = []
        safe.update(
            {
                "source_kind": "planned",
                "status": "planned",
                "evidence_kind": "none",
                "evidence_level": "L0",
                "canonical_evidence_level": "L0",
                "archive_evidence_level": "L0",
                "fact_eligibility": "schedule_commitment" if schedule_trusted else "none",
            }
        )
        source_refs = [_text(item, 240) for item in (safe.get("source_refs") or []) if _text(item, 240)]
        if not source_refs:
            source_refs = [f"archive:{_text(idempotency_key, 240)}"]
    elif memory_type in {
        "bot_window_snapshot",
        "bot_schedule_reconciliation",
        "bot_detail_fragment",
        "bot_calendar_event",
    }:
        raw_status = _text(safe.get("status"), 32).lower()
        raw_evidence_kind = _text(safe.get("evidence_kind"), 48).lower()
        raw_eligibility = _text(safe.get("fact_eligibility"), 48).lower()
        if memory_type == "bot_window_snapshot":
            if raw_status and raw_status != "reconciled":
                safe["legacy_status"] = raw_status
            safe.update(
                {
                    "status": "reconciled",
                    "evidence_kind": "none",
                    "evidence_level": "L0",
                    "canonical_evidence_level": "L0",
                    "archive_evidence_level": "L0",
                    "epistemic_status": "inferred",
                    "fact_eligibility": "none",
                }
            )
        elif memory_type == "bot_schedule_reconciliation":
            compatible_fact = (
                raw_evidence_kind in {"interaction", "tool_action", "external_record"}
                and raw_eligibility in {"current_observed", "history_observed"}
                and bool(safe.get("source_refs_trusted") or safe.get("authority_verified"))
            )
            if not compatible_fact:
                if raw_status and raw_status != "reconciled":
                    safe["legacy_status"] = raw_status
                safe.update(
                    {
                        "status": "reconciled",
                        "evidence_kind": "none",
                        "evidence_level": "L0",
                        "canonical_evidence_level": "L0",
                        "archive_evidence_level": "L0",
                        "epistemic_status": "inferred",
                        "fact_eligibility": "none",
                    }
                )
        elif memory_type == "bot_detail_fragment":
            if raw_status and raw_status != "planned":
                safe["legacy_status"] = raw_status
            flags = [
                _text(item, 64)
                for item in (safe.get("legacy_flags") or [])
                if _text(item, 64)
            ]
            for flag in ("short_ttl_candidate", "unverified_plan"):
                if flag not in flags:
                    flags.append(flag)
            safe.update(
                {
                    "status": "planned",
                    "evidence_kind": "none",
                    "evidence_level": "L0",
                    "canonical_evidence_level": "L0",
                    "archive_evidence_level": "L0",
                    "epistemic_status": "inferred",
                    "content_granularity": "scene",
                    "materialization_state": "candidate",
                    "fact_eligibility": "none",
                    "legacy_flags": flags,
                }
            )
            if not _text(safe.get("expires_at"), 96):
                safe["expires_at"] = (current + timedelta(hours=2)).isoformat(timespec="seconds")
        else:  # bot_calendar_event
            trusted_calendar = _has_trusted_schedule_ref(safe, "calendar", current)
            safe.update(
                {
                    "status": "planned",
                    "evidence_kind": "external_commitment",
                    "epistemic_status": "asserted",
                    "content_granularity": "commitment",
                    "materialization_state": "none",
                    "fact_eligibility": "schedule_commitment" if trusted_calendar else "none",
                    "commitment_level": "confirmed" if trusted_calendar else "tentative",
                }
            )
    # The archive contract only stores L0-L3.  Keep the original local level
    # separately so lossy L4/L5 writes cannot be promoted on a later read.
    requested_evidence = _text(safe.get("canonical_evidence_level") or safe.get("evidence_level"), 8).upper()
    canonical_evidence = requested_evidence if requested_evidence in {"L0", "L1", "L2", "L3", "L4", "L5"} else default_evidence
    evidence = "L0" if memory_type == "bot_schedule_plan" else (canonical_evidence if canonical_evidence in {"L0", "L1", "L2", "L3"} else "L3")
    evidence_mapping = safe.get("evidence_level_mapping") if isinstance(safe.get("evidence_level_mapping"), dict) else {}
    if not evidence_mapping:
        evidence_mapping = {
            "canonical_evidence_level": canonical_evidence,
            "archive_evidence_level": evidence,
            "lossy": canonical_evidence != evidence,
        }
    created_at = _text(safe.get("created_at"), 80) or current.isoformat(timespec="seconds")
    updated_at = _text(safe.get("updated_at"), 80) or created_at
    canonical_key = _text(idempotency_key, 240)
    identity = (
        f"{BOT_PERSONAL_MEMORY_DOMAIN}|{owner_bot_id}|{persona_id}|{memory_type}|{canonical_key}"
        if requested_canonical_schema >= BOT_PERSONAL_CANONICAL_SCHEMA_VERSION
        else f"{BOT_PERSONAL_MEMORY_DOMAIN}|{memory_type}|{canonical_key}"
    )
    record_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    record_id = f"botmem_{record_digest}"
    archive_status = (
        "planned"
        if memory_type in {"bot_schedule_plan", "bot_detail_fragment", "bot_calendar_event"}
        else "reconciled"
        if memory_type in {"bot_window_snapshot", "bot_schedule_reconciliation"}
        else (_text(safe.get("status"), 32) or default_status)
    )
    archive_authority = _text(safe.get("authority_kind"), 48) or (
        "llm"
        if memory_type in {"bot_schedule_plan", "bot_detail_fragment"}
        else "calendar"
        if memory_type == "bot_calendar_event"
        else "state"
    )
    archive_commitment = _text(safe.get("commitment_level"), 24) or ("tentative" if memory_type == "bot_schedule_plan" else "tentative")
    if memory_type == "bot_schedule_plan" and not schedule_trusted:
        if archive_authority in {"calendar", "timetable", "roster", "appointment", "user_confirmation"}:
            safe["legacy_authority_kind"] = archive_authority
            archive_authority = "llm"
        if archive_commitment == "confirmed":
            safe["legacy_commitment_level"] = archive_commitment
            archive_commitment = "tentative"
    if memory_type == "bot_schedule_plan":
        safe["authority_kind"] = archive_authority
        safe["commitment_level"] = archive_commitment
        safe["epistemic_status"] = "inferred"
        safe["content_granularity"] = "intent"
        safe["materialization_state"] = "none"
    return BotPersonalArchiveDTO(
        record_id=record_id,
        memory_domain=BOT_PERSONAL_MEMORY_DOMAIN,
        memory_type=memory_type,
        subject=BOT_PERSONAL_SUBJECT,
        date=date_key,
        window=window,
        window_date=date_key,
        occurred_at=occurred,
        created_at=created_at,
        updated_at=updated_at,
        source_kind=source_kind,
        source_refs=source_refs,
        certainty=_certainty(safe.get("certainty"), 0.6),
        evidence_level=evidence,
        status=archive_status,
        version=max(1, int(version or 1)),
        idempotency_key=_text(idempotency_key, 240),
        payload_schema_version=BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION,
        payload=deepcopy(safe),
        evidence_kind=_text(safe.get("evidence_kind"), 48) or (
            "none" if memory_type in {"bot_schedule_plan", "bot_window_snapshot", "bot_schedule_reconciliation", "bot_detail_fragment"}
            else "external_commitment" if memory_type == "bot_calendar_event" else "observed"
        ),
        canonical_evidence_level=canonical_evidence,
        archive_evidence_level=evidence,
        evidence_level_mapping=deepcopy(evidence_mapping),
        authority_kind=archive_authority,
        commitment_level=archive_commitment,
        epistemic_status=_text(safe.get("epistemic_status"), 24) or ("inferred" if memory_type == "bot_schedule_plan" else "observed"),
        content_granularity=_text(safe.get("content_granularity"), 24) or "intent",
        materialization_state=_text(safe.get("materialization_state"), 24) or "none",
        fact_eligibility=_text(safe.get("fact_eligibility"), 48) or "none",
        actor_type=_text(safe.get("actor_type"), 32) or "bot",
        subject_actor_id=_text(safe.get("subject_actor_id"), 120) or BOT_PERSONAL_SUBJECT,
        object_actor_id=_text(safe.get("object_actor_id"), 120),
        source_actor_id=_text(safe.get("source_actor_id"), 120) or "system",
        target_user_id=_text(safe.get("target_user_id"), 120),
        participant_roles=deepcopy(safe.get("participant_roles")) if isinstance(safe.get("participant_roles"), list) else [],
        runtime_origin_refs=deepcopy(safe.get("runtime_origin_refs")) if isinstance(safe.get("runtime_origin_refs"), list) else [],
        expires_at=_text(safe.get("expires_at"), 96),
        decision_trace=deepcopy(safe.get("decision_trace")) if isinstance(safe.get("decision_trace"), list) else [],
        owner_bot_id=owner_bot_id,
        persona_id=persona_id,
        canonical_schema_version=requested_canonical_schema,
    )


def envelope_size_bytes(envelope: dict[str, Any]) -> int:
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
