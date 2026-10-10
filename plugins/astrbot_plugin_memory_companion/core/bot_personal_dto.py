from __future__ import annotations

"""Structured, privacy-limited DTOs for the Bot Personal archive boundary."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from . import bot_personal_contract
from .bot_personal_contract import (
    BOT_PERSONAL_CANONICAL_SCHEMA_VERSION,
    BOT_PERSONAL_LEGACY_CANONICAL_SCHEMA_VERSIONS,
    BOT_PERSONAL_MEMORY_DOMAIN,
    BOT_PERSONAL_MEMORY_TYPES,
    BOT_PERSONAL_MAX_PAYLOAD_BYTES,
    BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION,
    BOT_PERSONAL_SUBJECT,
    BOT_PERSONAL_WINDOWS,
    TYPE_CONTRACTS,
    WINDOW_SLUGS,
)


BOT_PERSONAL_MAX_RECORD_VERSION = 1_000_000

# Canonical C3 agenda fields are additive to the archive envelope.  The
# archive's legacy evidence column only stores L0-L3; L4/L5 remain visible in
# ``canonical_evidence_level`` together with an explicit lossy mapping.
CANONICAL_SCHEMA_VERSION = BOT_PERSONAL_CANONICAL_SCHEMA_VERSION
CANONICAL_EVIDENCE_LEVELS = frozenset({"L0", "L1", "L2", "L3", "L4", "L5"})
ARCHIVE_EVIDENCE_LEVELS = frozenset({"L0", "L1", "L2", "L3"})
EVIDENCE_KINDS = frozenset({
    "none", "interaction", "self_state_commit", "tool_action",
    "external_record", "external_commitment",
})
AUTHORITY_KINDS = frozenset({
    "calendar", "timetable", "roster", "appointment", "user_confirmation",
    "routine", "persona", "state", "llm",
})
COMMITMENT_LEVELS = frozenset({"confirmed", "routine", "tentative"})
EPISTEMIC_STATUSES = frozenset({"asserted", "inferred", "observed"})
CONTENT_GRANULARITIES = frozenset({"commitment", "intent", "candidate", "scene"})
MATERIALIZATION_STATES = frozenset({"none", "candidate", "active", "rejected", "expired"})
FACT_ELIGIBILITIES = frozenset({
    "none", "schedule_commitment", "current_internal", "current_observed",
    "history_observed",
})
EXTERNAL_AUTHORITIES = frozenset({"calendar", "timetable", "roster", "appointment", "user_confirmation"})


class BotPersonalValidationError(ValueError):
    def __init__(self, error_code: str, field: str = "", message: str = "") -> None:
        self.error_code = str(error_code or "invalid").strip()[:80]
        self.field = str(field or "").strip()[:160]
        self.message = str(message or self.error_code).strip()[:300]
        super().__init__(self.message)


_FORBIDDEN_KEY_PARTS = (
    "prompt", "conversation", "transcript", "message_chain", "message_history", "raw_message",
    "messages",
    "raw_prompt", "chat_history", "cookie", "token", "secret", "credential", "password",
    "passwd", "authorization", "bearer", "api_key", "apikey", "private_key", "base64",
    "binary", "media_bytes", "image_bytes", "audio_bytes", "video_bytes", "media_binary",
    "media_data", "media_content", "media_blob", "file_path", "filepath", "absolute_path",
    "local_path", "raw_payload",
)
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)^bearer\s+\S+"),
    re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)base64,"),
    re.compile(r"(?i)^data:[a-z0-9.+-]+/[a-z0-9.+-]+[;,]"),
    re.compile(r"(?i)^-----begin "),
    re.compile(r"(?i)(?:^|[\s=:;,])(?:[A-Za-z]:[\\/]|\\\\|/home/|/root/|/tmp/|/var/|/volume\d+/)"),
)
_FORBIDDEN_TOP_LEVEL_KEYS = frozenset({
    "conversation", "transcript", "message_chain", "raw_prompt", "raw_payload", "payload_raw",
})


def _key_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _check_string(value: str, path: str) -> str:
    text = _text(value, BOT_PERSONAL_MAX_PAYLOAD_BYTES)
    if any(pattern.search(text) for pattern in _SENSITIVE_VALUE_PATTERNS):
        raise BotPersonalValidationError("privacy_rejected", path, "credential, binary media or absolute path is not accepted")
    return text


def sanitize_bot_personal_value(value: Any, *, path: str = "payload", depth: int = 0) -> Any:
    if depth > 8:
        raise BotPersonalValidationError("privacy_rejected", path, "payload nesting is too deep")
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise BotPersonalValidationError("payload_too_large", path, "too many fields")
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, 80)
            normalized = _key_name(key)
            if not key:
                raise BotPersonalValidationError("invalid", path, "empty field name")
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                raise BotPersonalValidationError("privacy_rejected", f"{path}.{key}", "sensitive or raw field is not accepted")
            result[key] = sanitize_bot_personal_value(raw_value, path=f"{path}.{key}", depth=depth + 1)
        return result
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise BotPersonalValidationError("privacy_rejected", path, "binary media is not accepted")
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise BotPersonalValidationError("payload_too_large", path, "too many list items")
        return [sanitize_bot_personal_value(item, path=f"{path}[{index}]", depth=depth + 1) for index, item in enumerate(value)]
    if isinstance(value, str):
        return _check_string(value, path)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise BotPersonalValidationError("invalid", path, "value is not JSON serializable")


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise BotPersonalValidationError("invalid", "payload", f"payload is not JSON serializable: {exc}") from exc


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BotPersonalValidationError("invalid", field, f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BotPersonalValidationError("invalid", field, f"{field} requires a timezone")
    return parsed


def _canonical_text(value: Any, choices: frozenset[str], default: str) -> str:
    candidate = _text(value, 80).lower()
    return candidate if candidate in choices else default


def _string_refs(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        reference = _text(item, 240)
        if reference and reference not in result:
            result.append(reference)
        if len(result) >= 30:
            break
    return result


def _parse_optional_moment(value: Any, timezone_name: str = "") -> datetime | None:
    """Parse a trusted-reference timestamp without accepting naive clocks."""

    if isinstance(value, datetime):
        parsed = value
    else:
        text = _text(value, 96)
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not timezone_name:
            return None
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            return None
    elif timezone_name:
        try:
            parsed = parsed.astimezone(ZoneInfo(timezone_name))
        except Exception:
            return None
    return parsed


def _iso_moment(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else ""


def _expected_schedule_ref_id(
    namespace: str,
    event_id: str,
    revision: str,
    provider: str,
    subject_actor_id: str,
    *,
    updated_at: str = "",
    timezone: str = "",
    effective_from: str = "",
    effective_to: str = "",
    expires_at: str = "",
    authority_kind: str = "",
    confirmation_event_id: str = "",
    confirmation_actor_id: str = "",
    proposition: str = "",
    confirmed_at: str = "",
    target_user_id: str = "",
) -> str:
    # Keep this verifier-side canonicalization byte-for-byte compatible with
    # schedule_authority._ref_id in the companion plugin.
    raw = json.dumps(
        {
            "namespace": namespace,
            "event_id": event_id,
            "revision": revision,
            "provider": provider,
            "subject_actor_id": subject_actor_id,
            "updated_at": updated_at,
            "timezone": timezone,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "expires_at": expires_at,
            "authority_kind": authority_kind,
            "confirmation_event_id": confirmation_event_id,
            "confirmation_actor_id": confirmation_actor_id,
            "proposition": proposition,
            "confirmed_at": confirmed_at,
            "target_user_id": target_user_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "trusted_schedule:" + hashlib.sha256(raw).hexdigest()[:24]


def _has_trusted_schedule_ref(payload: Mapping[str, Any], authority: str, now: datetime) -> bool:
    """Validate an adapter-shaped schedule reference, never a trust boolean.

    The memory plugin intentionally does not own the authority adapter.  It
    can still reject forged references by checking the adapter's stable id,
    actor binding, absolute interval, state, and expiry before accepting a
    ``confirmed`` commitment.
    """

    schedule_ref = payload.get("schedule_ref")
    if not isinstance(schedule_ref, Mapping):
        return False
    ref_id = _text(schedule_ref.get("ref_id"), 240)
    source_refs = set(_string_refs(payload.get("source_refs")))
    if not ref_id or ref_id not in source_refs:
        return False
    if any(
        schedule_ref.get(key) is False
        or _text(schedule_ref.get(key), 16).lower() in {"false", "no", "denied", "invalid", "revoked"}
        for key in ("authorized", "permission_valid", "subject_authorized")
        if key in schedule_ref
    ):
        return False
    declared_authority = _text(schedule_ref.get("authority_kind"), 48).lower()
    if declared_authority != _text(authority, 48).lower() or declared_authority not in EXTERNAL_AUTHORITIES:
        return False
    subject = _text(schedule_ref.get("subject_actor_id"), 120)
    if subject != BOT_PERSONAL_SUBJECT:
        return False
    namespace = _text(schedule_ref.get("namespace"), 120)
    event_id = _text(schedule_ref.get("event_id"), 160)
    provider = _text(schedule_ref.get("provider"), 120)
    revision = _text(schedule_ref.get("revision"), 80)
    timezone_name = _text(schedule_ref.get("timezone"), 80)
    if not namespace or not event_id or not provider or not revision or not timezone_name:
        return False
    try:
        ZoneInfo(timezone_name)
    except Exception:
        return False
    updated = _parse_optional_moment(schedule_ref.get("updated_at"), timezone_name)
    start = _parse_optional_moment(schedule_ref.get("effective_from"), timezone_name)
    end = _parse_optional_moment(schedule_ref.get("effective_to"), timezone_name)
    if updated is None or start is None or end is None or end <= start:
        return False
    expires = _parse_optional_moment(schedule_ref.get("expires_at"), timezone_name)
    if schedule_ref.get("expires_at") not in (None, "") and expires is None:
        return False
    confirmation_event_id = _text(schedule_ref.get("confirmation_event_id"), 160)
    confirmation_actor_id = _text(
        schedule_ref.get("confirmation_actor_id")
        or schedule_ref.get("confirmed_by")
        or schedule_ref.get("source_actor_id"),
        120,
    )
    proposition = _text(
        schedule_ref.get("proposition")
        or schedule_ref.get("title")
        or schedule_ref.get("activity")
        or schedule_ref.get("summary"),
        240,
    )
    confirmed_at = _parse_optional_moment(
        schedule_ref.get("confirmed_at") or schedule_ref.get("confirmation_time"),
        timezone_name,
    )
    expected_ref_id = _expected_schedule_ref_id(
        namespace,
        event_id,
        revision,
        provider,
        subject,
        updated_at=_iso_moment(updated),
        timezone=timezone_name,
        effective_from=_iso_moment(start),
        effective_to=_iso_moment(end),
        expires_at=_iso_moment(expires),
        authority_kind=declared_authority,
        confirmation_event_id=confirmation_event_id,
        confirmation_actor_id=confirmation_actor_id,
        proposition=proposition,
        confirmed_at=_iso_moment(confirmed_at),
        target_user_id=_text(schedule_ref.get("target_user_id"), 120),
    )
    if ref_id != expected_ref_id:
        return False
    if declared_authority == "user_confirmation":
        if not confirmation_event_id or not confirmation_actor_id or confirmation_actor_id == subject or not proposition or confirmed_at is None:
            return False
    state = _text(schedule_ref.get("state"), 24).lower() or "active"
    if state != "active":
        return False
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    try:
        current = current.astimezone(ZoneInfo(timezone_name))
    except Exception:
        return False
    return expires is None or current < expires


def _canonical_mapping(canonical: str, archive: str) -> dict[str, Any]:
    return {
        "canonical_evidence_level": canonical,
        "archive_evidence_level": archive,
        "lossy": canonical != archive,
    }


def _raw_field(source: Mapping[str, Any], payload: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = source.get(key)
    if value in (None, ""):
        value = payload.get(key, default)
    return value


def _derive_window(occurred_at: str) -> str:
    hour = _parse_datetime(occurred_at, "occurred_at").hour
    minutes = hour * 60 + _parse_datetime(occurred_at, "occurred_at").minute
    return bot_personal_contract.window_for_minutes(minutes)


def _record_id(
    memory_type: str,
    idempotency_key: str,
    *,
    canonical_schema_version: int = 1,
    owner_bot_id: str = "",
    persona_id: str = "",
) -> str:
    identity = (
        f"{BOT_PERSONAL_MEMORY_DOMAIN}|{owner_bot_id}|{persona_id}|{memory_type}|{idempotency_key}"
        if canonical_schema_version >= BOT_PERSONAL_CANONICAL_SCHEMA_VERSION
        else f"{BOT_PERSONAL_MEMORY_DOMAIN}|{memory_type}|{idempotency_key}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"botmem_{digest}"


def _fingerprint(data: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_bot_personal_idempotency_key(value: Any) -> str:
    if not isinstance(value, str):
        raise BotPersonalValidationError("invalid", "idempotency_key", "idempotency_key must be a string")
    key = _text(value, 240)
    if not key or any(ord(char) < 32 for char in key):
        raise BotPersonalValidationError("invalid", "idempotency_key", "idempotency_key is required and must be printable")
    normalized = _key_name(key)
    if any(part in normalized for part in _FORBIDDEN_KEY_PARTS) or any(pattern.search(key) for pattern in _SENSITIVE_VALUE_PATTERNS):
        raise BotPersonalValidationError("privacy_rejected", "idempotency_key", "unsafe idempotency_key")
    return key


@dataclass(frozen=True)
class BotPersonalArchiveDTO:
    record_id: str
    memory_domain: str
    memory_type: str
    subject: str
    date: str
    window: str
    occurred_at: str
    source_kind: str
    source_refs: tuple[str, ...]
    certainty: float
    evidence_level: str
    status: str
    version: int
    idempotency_key: str
    payload_schema_version: str
    payload: dict[str, Any]
    # Canonical C3 agenda axes.  Defaults keep old positional construction and
    # old consumers valid while new readers can inspect the normalized view.
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
    runtime_origin_refs: tuple[str, ...] = ()
    expires_at: str = ""
    decision_trace: tuple[dict[str, Any], ...] = ()
    owner_bot_id: str = ""
    persona_id: str = ""
    canonical_schema_version: int = CANONICAL_SCHEMA_VERSION

    def envelope(self) -> dict[str, Any]:
        result = {
            "record_id": self.record_id,
            "memory_domain": self.memory_domain,
            "memory_type": self.memory_type,
            "subject": self.subject,
            "date": self.date,
            "window": self.window,
            "occurred_at": self.occurred_at,
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
            "runtime_origin_refs": list(self.runtime_origin_refs),
            "expires_at": self.expires_at,
            "decision_trace": deepcopy(list(self.decision_trace)),
            "canonical_schema_version": self.canonical_schema_version,
        }
        if self.canonical_schema_version >= BOT_PERSONAL_CANONICAL_SCHEMA_VERSION:
            result["owner_bot_id"] = self.owner_bot_id
            result["persona_id"] = self.persona_id
        return result

    to_dict = envelope


def build_bot_personal_archive(
    envelope: BotPersonalArchiveDTO | Mapping[str, Any] | None = None,
    *,
    memory_type: str | None = None,
    payload: Mapping[str, Any] | None = None,
    record_id: str = "",
    memory_domain: str | None = None,
    subject: str | None = None,
    date: str = "",
    window: str = "",
    occurred_at: str = "",
    source_kind: str = "",
    source_refs: Any = None,
    certainty: Any = None,
    evidence_level: str = "",
    status: str = "",
    version: Any = None,
    idempotency_key: str = "",
    payload_schema_version: str | None = None,
    now: datetime | None = None,
    evidence_kind: str = "",
    canonical_evidence_level: str = "",
    archive_evidence_level: str = "",
    evidence_level_mapping: Any = None,
    authority_kind: str = "",
    commitment_level: str = "",
    epistemic_status: str = "",
    content_granularity: str = "",
    materialization_state: str = "",
    fact_eligibility: str = "",
    actor_type: str = "",
    subject_actor_id: str = "",
    object_actor_id: str = "",
    source_actor_id: str = "",
    target_user_id: str = "",
    participant_roles: Any = None,
    runtime_origin_refs: Any = None,
    expires_at: str = "",
    decision_trace: Any = None,
    owner_bot_id: str = "",
    persona_id: str = "",
    canonical_schema_version: Any = None,
) -> BotPersonalArchiveDTO:
    if isinstance(envelope, BotPersonalArchiveDTO):
        return envelope
    source: dict[str, Any] = dict(envelope or {}) if isinstance(envelope, Mapping) else {}
    if envelope is not None and not isinstance(envelope, (BotPersonalArchiveDTO, Mapping)):
        raise BotPersonalValidationError("invalid", "envelope", "envelope must be an object")
    supplied = {key: value for key, value in {
        "memory_type": memory_type, "payload": payload, "record_id": record_id, "memory_domain": memory_domain,
        "subject": subject, "date": date, "window": window, "occurred_at": occurred_at, "source_kind": source_kind,
        "source_refs": source_refs, "certainty": certainty, "evidence_level": evidence_level, "status": status,
        "version": version, "idempotency_key": idempotency_key, "payload_schema_version": payload_schema_version,
        "evidence_kind": evidence_kind, "canonical_evidence_level": canonical_evidence_level,
        "archive_evidence_level": archive_evidence_level, "evidence_level_mapping": evidence_level_mapping,
        "authority_kind": authority_kind, "commitment_level": commitment_level,
        "epistemic_status": epistemic_status, "content_granularity": content_granularity,
        "materialization_state": materialization_state, "fact_eligibility": fact_eligibility,
        "actor_type": actor_type, "subject_actor_id": subject_actor_id, "object_actor_id": object_actor_id,
        "source_actor_id": source_actor_id, "target_user_id": target_user_id,
        "participant_roles": participant_roles, "runtime_origin_refs": runtime_origin_refs,
        "expires_at": expires_at, "decision_trace": decision_trace,
        "owner_bot_id": owner_bot_id, "persona_id": persona_id,
        "canonical_schema_version": canonical_schema_version,
    }.items() if value not in (None, "")}
    source.update(supplied)
    for key in source:
        if _key_name(key) in _FORBIDDEN_TOP_LEVEL_KEYS or any(part in _key_name(key) for part in _FORBIDDEN_KEY_PARTS):
            raise BotPersonalValidationError("privacy_rejected", str(key), "sensitive or raw field is not accepted")

    kind = _text(source.get("memory_type"), 80).lower()
    if kind not in BOT_PERSONAL_MEMORY_TYPES or kind not in TYPE_CONTRACTS:
        raise BotPersonalValidationError("invalid", "memory_type", "unknown Bot Personal memory type")
    if _text(source.get("memory_domain"), 80) not in ("", BOT_PERSONAL_MEMORY_DOMAIN):
        raise BotPersonalValidationError("invalid", "memory_domain", "Bot Personal domain is fixed")
    if _text(source.get("subject"), 80) not in ("", BOT_PERSONAL_SUBJECT):
        raise BotPersonalValidationError("invalid", "subject", "Bot Personal subject is fixed")

    key = validate_bot_personal_idempotency_key(source.get("idempotency_key"))
    try:
        requested_canonical_schema = int(source.get("canonical_schema_version") or 1)
    except (TypeError, ValueError) as exc:
        raise BotPersonalValidationError(
            "invalid", "canonical_schema_version", "canonical schema must be numeric"
        ) from exc
    supported_canonical_schemas = {
        *BOT_PERSONAL_LEGACY_CANONICAL_SCHEMA_VERSIONS,
        BOT_PERSONAL_CANONICAL_SCHEMA_VERSION,
    }
    if requested_canonical_schema not in supported_canonical_schemas:
        raise BotPersonalValidationError(
            "invalid", "canonical_schema_version", "unsupported canonical schema"
        )
    owner_bot_id = _check_string(_text(source.get("owner_bot_id"), 120), "owner_bot_id")
    persona_id = _check_string(_text(source.get("persona_id"), 96), "persona_id")
    if requested_canonical_schema >= BOT_PERSONAL_CANONICAL_SCHEMA_VERSION:
        if not owner_bot_id or not persona_id:
            raise BotPersonalValidationError(
                "invalid_namespace",
                "owner_bot_id",
                "current Bot Personal schema requires owner_bot_id and persona_id",
            )
    else:
        owner_bot_id = ""
        persona_id = "legacy"
    derived_record_id = _record_id(
        kind,
        key,
        canonical_schema_version=requested_canonical_schema,
        owner_bot_id=owner_bot_id,
        persona_id=persona_id,
    )
    supplied_record_id = source.get("record_id")
    if supplied_record_id not in (None, ""):
        if not isinstance(supplied_record_id, str):
            raise BotPersonalValidationError("invalid", "record_id", "record_id must be a string")
        record_id = _check_string(_text(supplied_record_id, 120), "record_id")
        if record_id != derived_record_id:
            raise BotPersonalValidationError("invalid", "record_id", "record_id must match the idempotency key")
    raw_payload = source.get("payload")
    if not isinstance(raw_payload, Mapping):
        raise BotPersonalValidationError("invalid", "payload", "payload must be an object")
    safe_payload = sanitize_bot_personal_value(raw_payload, path="payload")
    if _json_size(safe_payload) > BOT_PERSONAL_MAX_PAYLOAD_BYTES:
        raise BotPersonalValidationError("payload_too_large", "payload", "payload exceeds the contract limit")
    safe: dict[str, Any] = dict(safe_payload)
    # New producers may put canonical axes beside the legacy envelope while
    # older producers keep them in ``payload``.  Normalize both forms through
    # the same gate; top-level values take precedence.
    canonical_input_fields = (
        "evidence_kind", "canonical_evidence_level", "archive_evidence_level",
        "evidence_level_mapping", "authority_kind", "commitment_level",
        "epistemic_status", "content_granularity", "materialization_state",
        "fact_eligibility", "actor_type", "subject_actor_id", "object_actor_id",
        "source_actor_id", "target_user_id", "participant_roles", "runtime_origin_refs",
        "expires_at", "decision_trace", "schedule_ref", "source_refs_trusted",
        "trusted_source_refs", "authority_verified", "legacy_status", "legacy_source_kind",
    )
    for field in canonical_input_fields:
        if field not in source:
            continue
        safe[field] = sanitize_bot_personal_value(source[field], path=field)
    for field in ("source_kind", "status", "lifecycle_status", "evidence_level"):
        if field in source:
            safe[field] = _text(source[field], 96)

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    occurred = _text(_raw_field(source, safe, "occurred_at", ""), 80)
    if not occurred:
        occurred = current.isoformat(timespec="seconds")
    _parse_datetime(occurred, "occurred_at")
    record_date = _text(_raw_field(source, safe, "date", ""), 20) or occurred[:10]
    try:
        datetime.strptime(record_date, "%Y-%m-%d")
    except ValueError as exc:
        raise BotPersonalValidationError("invalid", "date", "date must be YYYY-MM-DD") from exc
    record_window = _text(_raw_field(source, safe, "window", ""), 40).lower() or _derive_window(occurred)
    if record_window not in BOT_PERSONAL_WINDOWS or record_window not in WINDOW_SLUGS:
        raise BotPersonalValidationError("invalid", "window", "window is outside the shared contract")
    try:
        confidence = float(source.get("certainty", 0.6))
    except (TypeError, ValueError) as exc:
        raise BotPersonalValidationError("invalid", "certainty", "certainty must be numeric") from exc
    if not 0.0 <= confidence <= 1.0:
        raise BotPersonalValidationError("invalid", "certainty", "certainty must be between 0 and 1")
    try:
        record_version = int(source.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise BotPersonalValidationError("invalid", "version", "version must be a positive integer") from exc
    if not 1 <= record_version <= BOT_PERSONAL_MAX_RECORD_VERSION:
        raise BotPersonalValidationError(
            "invalid",
            "version",
            f"version must be between 1 and {BOT_PERSONAL_MAX_RECORD_VERSION}",
        )
    schema = _text(source.get("payload_schema_version"), 40) or BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION
    if schema != BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION:
        raise BotPersonalValidationError("invalid", "payload_schema_version", "unsupported payload schema")
    refs_value = source.get("source_refs")
    if refs_value is None:
        refs_value = safe.get("source_refs")
    if refs_value is None:
        refs_value = [f"archive:{key}"]
    if not isinstance(refs_value, (list, tuple)):
        raise BotPersonalValidationError("invalid", "source_refs", "source_refs must be a list")
    refs_list: list[str] = []
    for item in refs_value:
        if not isinstance(item, str):
            raise BotPersonalValidationError("invalid", "source_refs", "source_refs must contain strings")
        reference = _check_string(_text(item, 240), "source_refs")
        if reference:
            refs_list.append(reference)
    refs = tuple(refs_list)
    if not refs:
        raise BotPersonalValidationError("invalid", "source_refs", "source_refs must not be empty")
    safe["source_refs"] = list(refs)

    contract_source, contract_evidence, contract_status = TYPE_CONTRACTS[kind]
    # Actor binding is deliberately strict at the Bot Personal boundary.  A
    # user assertion may be archived by another domain, but it must not be
    # silently rewritten as Bot history.
    explicit_subject = _raw_field(source, safe, "subject_actor_id", "")
    subject_actor_id = _text(explicit_subject, 120) or BOT_PERSONAL_SUBJECT
    if subject_actor_id != BOT_PERSONAL_SUBJECT:
        raise BotPersonalValidationError("invalid", "subject_actor_id", "Bot Personal subject actor is fixed")
    safe["subject_actor_id"] = BOT_PERSONAL_SUBJECT
    actor_type = _canonical_text(_raw_field(source, safe, "actor_type", "bot"), frozenset({"bot"}), "bot")
    source_actor_id = _text(_raw_field(source, safe, "source_actor_id", "system"), 120) or "system"
    object_actor_id = _text(_raw_field(source, safe, "object_actor_id", ""), 120)
    target_user_id = _text(_raw_field(source, safe, "target_user_id", ""), 120)
    participant_roles = _raw_field(source, safe, "participant_roles", [])
    if not isinstance(participant_roles, list):
        participant_roles = list(participant_roles) if isinstance(participant_roles, (tuple, set)) else []
    participant_roles = participant_roles[:30]
    runtime_origin_refs = tuple(_string_refs(_raw_field(source, safe, "runtime_origin_refs", [])))
    expires_at = _text(_raw_field(source, safe, "expires_at", ""), 96)

    # Preserve legacy input for diagnostics, then overwrite executable axes by
    # memory type.  A model-supplied status/source/evidence level is never an
    # execution proof.
    trace: list[dict[str, Any]] = []
    raw_trace = _raw_field(source, safe, "decision_trace", [])
    if isinstance(raw_trace, (list, tuple)):
        for item in raw_trace[:50]:
            if isinstance(item, Mapping):
                trace.append(deepcopy(dict(item)))
            elif _text(item, 240):
                trace.append({"code": "legacy_trace", "message": _text(item, 240)})

    requested_source_kind = _text(_raw_field(source, safe, "source_kind", ""), 40).lower()
    requested_status = _text(_raw_field(source, safe, "status", ""), 40).lower()
    requested_evidence_kind = _canonical_text(
        _raw_field(source, safe, "evidence_kind", ""), EVIDENCE_KINDS, "none"
    )
    requested_authority = _canonical_text(
        _raw_field(source, safe, "authority_kind", ""), AUTHORITY_KINDS,
        "calendar" if kind == "bot_calendar_event" else "llm",
    )
    requested_commitment = _canonical_text(
        _raw_field(source, safe, "commitment_level", ""), COMMITMENT_LEVELS, "tentative"
    )
    requested_granularity = _canonical_text(
        _raw_field(source, safe, "content_granularity", ""), CONTENT_GRANULARITIES, "intent"
    )
    requested_materialization = _canonical_text(
        _raw_field(source, safe, "materialization_state", ""), MATERIALIZATION_STATES, "none"
    )
    requested_fact = _canonical_text(
        _raw_field(source, safe, "fact_eligibility", ""), FACT_ELIGIBILITIES, "none"
    )
    requested_canonical = _text(
        _raw_field(source, safe, "canonical_evidence_level", ""), 8
    ).upper()
    legacy_evidence_level = _text(_raw_field(source, safe, "evidence_level", ""), 8).upper()
    # ``evidence_level`` is a legacy field.  Keep its historical default for
    # old archive producers unless they explicitly provide the canonical axis.
    if requested_canonical not in CANONICAL_EVIDENCE_LEVELS:
        # L4/L5 never fit the legacy archive column, so accepting them from a
        # newer producer is useful even when it has not yet sent the additive
        # canonical key.  Keep the historical L0-L3 default behavior intact.
        requested_canonical = legacy_evidence_level if legacy_evidence_level in {"L4", "L5"} else contract_evidence
    explicit_canonical = bool(
        source.get("canonical_evidence_level") not in (None, "")
        or safe_payload.get("canonical_evidence_level") not in (None, "")
        or legacy_evidence_level in {"L4", "L5"}
    )
    if kind == "bot_schedule_plan":
        requested_canonical = "L0"
    archive_evidence = (
        requested_canonical
        if requested_canonical in ARCHIVE_EVIDENCE_LEVELS
        else "L3"
    )
    if not explicit_canonical and kind != "bot_schedule_plan":
        archive_evidence = contract_evidence

    trusted_schedule = _has_trusted_schedule_ref(safe, requested_authority, current)
    commitment_value = requested_commitment
    if kind == "bot_schedule_plan":
        if requested_status and requested_status != "planned":
            safe["legacy_status"] = requested_status
        if requested_source_kind and requested_source_kind != "planned":
            safe["legacy_source_kind"] = requested_source_kind
        archive_ref = f"archive:{key}"
        if refs and not trusted_schedule and refs != (archive_ref,):
            safe["legacy_source_refs"] = list(refs)[:30]
            refs = (archive_ref,)
        source_kind_value = "planned"
        status_value = "planned"
        evidence_kind_value = "none"
        requested_canonical = archive_evidence = "L0"
        requested_authority = requested_authority if trusted_schedule else (
            "routine" if requested_authority == "routine" else "llm"
        )
        commitment_value = (
            "confirmed" if trusted_schedule and requested_authority in EXTERNAL_AUTHORITIES
            else "routine" if requested_authority == "routine"
            else "tentative"
        )
        epistemic_value = "inferred"
        granularity_value = "intent"
        materialization_value = "none"
        fact_value = "schedule_commitment" if trusted_schedule else "none"
        if requested_status and requested_status != "planned" and not any(
            item.get("code") == "archive.plan_status_ignored" for item in trace
        ):
            trace.append({"code": "archive.plan_status_ignored", "requested": requested_status})
        if requested_source_kind and requested_source_kind != "planned" and not any(
            item.get("code") == "archive.plan_source_kind_ignored" for item in trace
        ):
            trace.append({"code": "archive.plan_source_kind_ignored", "requested": requested_source_kind})
        if requested_evidence_kind != "none" and not any(
            item.get("code") == "archive.plan_evidence_ignored" for item in trace
        ):
            trace.append({"code": "archive.plan_evidence_ignored", "requested": requested_evidence_kind})
        if requested_commitment == "confirmed" and commitment_value != "confirmed" and not any(
            item.get("code") == "archive.plan_commitment_downgraded" for item in trace
        ):
            trace.append({"code": "archive.plan_commitment_downgraded", "requested": "confirmed"})
        if not trusted_schedule and _text(_raw_field(source, safe, "authority_kind", ""), 48):
            if not any(item.get("code") == "archive.untrusted_schedule_authority" for item in trace):
                trace.append({"code": "archive.untrusted_schedule_authority", "requested": requested_authority})
    elif kind == "bot_window_snapshot":
        source_kind_value, status_value = "projection", "reconciled"
        evidence_kind_value = "none"
        requested_canonical = archive_evidence = "L0"
        requested_authority, commitment_value = "state", "tentative"
        epistemic_value, granularity_value = "inferred", "intent"
        materialization_value, fact_value = "none", "none"
    elif kind == "bot_schedule_reconciliation":
        source_kind_value, status_value = "reconciled", "reconciled"
        compatible = (
            requested_evidence_kind in {"interaction", "tool_action", "external_record"}
            and requested_fact in {"current_observed", "history_observed"}
            and bool(_raw_field(source, safe, "source_refs_trusted", False) or _raw_field(source, safe, "authority_verified", False))
            and bool(refs)
        )
        if not compatible:
            if requested_status and requested_status != "reconciled":
                safe["legacy_status"] = requested_status
            evidence_kind_value = "none"
            requested_canonical = archive_evidence = "L0"
            epistemic_value, fact_value = "inferred", "none"
            materialization_value = "none"
        else:
            evidence_kind_value = requested_evidence_kind
            epistemic_value, fact_value = "observed", requested_fact
            materialization_value = "active"
        granularity_value = requested_granularity
    elif kind == "bot_detail_fragment":
        source_kind_value, status_value = "detail", "planned"
        evidence_kind_value = "none"
        requested_canonical = archive_evidence = "L0"
        requested_authority, commitment_value = "llm", "tentative"
        epistemic_value, granularity_value = "inferred", "scene"
        materialization_value, fact_value = "candidate", "none"
        if not expires_at:
            expires_at = (current + timedelta(hours=2)).isoformat(timespec="seconds")
        flags = safe.get("legacy_flags") if isinstance(safe.get("legacy_flags"), list) else []
        flags = list(flags)
        for flag in ("short_ttl_candidate", "unverified_plan"):
            if flag not in flags:
                flags.append(flag)
        safe["legacy_flags"] = flags[:30]
    elif kind == "bot_calendar_event":
        source_kind_value, status_value = "calendar", "planned"
        evidence_kind_value = "external_commitment"
        requested_authority = "calendar"
        commitment_value = "confirmed" if trusted_schedule else "tentative"
        epistemic_value, granularity_value = "asserted", "commitment"
        materialization_value = "none"
        fact_value = "schedule_commitment" if trusted_schedule else "none"
    else:
        source_kind_value = contract_source
        status_value = (
            requested_status
            if kind == "bot_observed_activity"
            and requested_status in {"active", "completed", "partially_completed", "unknown", "cancelled"}
            else contract_status
        )
        evidence_kind_value = requested_evidence_kind
        if kind == "bot_observed_activity" and evidence_kind_value == "none":
            source_hint = _text(_raw_field(source, safe, "source", ""), 64).lower()
            evidence_kind_value = "interaction" if source_hint in {"chat", "conversation", "interaction", "message"} else "external_record"
        if evidence_kind_value in {"none", "external_commitment"}:
            fact_value = "none"
        elif evidence_kind_value == "self_state_commit":
            fact_value = "current_internal" if requested_fact == "current_internal" else "none"
        elif requested_fact in {"current_observed", "history_observed"} and refs:
            fact_value = requested_fact
        else:
            fact_value = "none"
        epistemic_value = "observed" if evidence_kind_value != "none" else "inferred"
        granularity_value = requested_granularity
        materialization_value = requested_materialization

    mapping = _canonical_mapping(requested_canonical, archive_evidence)
    safe.update(
        {
            "source_kind": source_kind_value,
            "status": status_value,
            "evidence_kind": evidence_kind_value,
            "evidence_level": archive_evidence,
            "canonical_evidence_level": requested_canonical,
            "archive_evidence_level": archive_evidence,
            "evidence_level_mapping": mapping,
            "authority_kind": requested_authority,
            "commitment_level": commitment_value,
            "epistemic_status": epistemic_value,
            "content_granularity": granularity_value,
            "materialization_state": materialization_value,
            "fact_eligibility": fact_value,
            "actor_type": actor_type,
            "subject_actor_id": subject_actor_id,
            "object_actor_id": object_actor_id,
            "source_actor_id": source_actor_id,
            "target_user_id": target_user_id,
            "participant_roles": deepcopy(participant_roles),
            "runtime_origin_refs": list(runtime_origin_refs),
            "expires_at": expires_at,
            "decision_trace": deepcopy(trace),
            "canonical_schema_version": requested_canonical_schema,
            "source_refs": list(refs),
        }
    )
    if _json_size(safe) > BOT_PERSONAL_MAX_PAYLOAD_BYTES:
        raise BotPersonalValidationError("payload_too_large", "payload", "payload exceeds the contract limit")
    return BotPersonalArchiveDTO(
        record_id=derived_record_id,
        memory_domain=BOT_PERSONAL_MEMORY_DOMAIN,
        memory_type=kind,
        subject=BOT_PERSONAL_SUBJECT,
        date=record_date,
        window=record_window,
        occurred_at=occurred,
        source_kind=source_kind_value,
        source_refs=refs,
        certainty=confidence,
        evidence_level=archive_evidence,
        status=status_value,
        version=record_version,
        idempotency_key=key,
        payload_schema_version=schema,
        payload=deepcopy(safe),
        evidence_kind=evidence_kind_value,
        canonical_evidence_level=requested_canonical,
        archive_evidence_level=archive_evidence,
        evidence_level_mapping=deepcopy(mapping),
        authority_kind=requested_authority,
        commitment_level=commitment_value,
        epistemic_status=epistemic_value,
        content_granularity=granularity_value,
        materialization_state=materialization_value,
        fact_eligibility=fact_value,
        actor_type=actor_type,
        subject_actor_id=subject_actor_id,
        object_actor_id=object_actor_id,
        source_actor_id=source_actor_id,
        target_user_id=target_user_id,
        participant_roles=deepcopy(participant_roles),
        runtime_origin_refs=runtime_origin_refs,
        expires_at=expires_at,
        decision_trace=tuple(deepcopy(trace)),
        owner_bot_id=owner_bot_id,
        persona_id=persona_id,
        canonical_schema_version=requested_canonical_schema,
    )


def bot_personal_payload_fingerprint(dto: BotPersonalArchiveDTO) -> str:
    payload = dto.envelope()
    payload.pop("record_id", None)
    return _fingerprint(payload)


__all__ = [
    "ARCHIVE_EVIDENCE_LEVELS", "AUTHORITY_KINDS", "CANONICAL_EVIDENCE_LEVELS",
    "CANONICAL_SCHEMA_VERSION", "COMMITMENT_LEVELS", "CONTENT_GRANULARITIES",
    "EVIDENCE_KINDS", "FACT_ELIGIBILITIES", "MATERIALIZATION_STATES",
    "BOT_PERSONAL_MAX_RECORD_VERSION", "BotPersonalArchiveDTO", "BotPersonalValidationError", "build_bot_personal_archive",
    "bot_personal_payload_fingerprint", "sanitize_bot_personal_value", "validate_bot_personal_idempotency_key",
]
