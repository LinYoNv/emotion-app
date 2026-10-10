"""Pure companion-issued, in-process one-shot P5 attestations.

An attestation handle is an opaque capability.  The registry retains only
identity anchors for the request, event, and P3 carrier plus bounded metadata
until TTL expiry or consumption.  It never inspects or serializes carrier
contents and never accepts raw prose, credentials, session text, or arbitrary
IDs as metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import secrets
import threading
import time
from typing import Callable, Iterable


ATTESTATION_SCHEMA_VERSION = "ops.p5.attestation.v1"
PROVENANCE_CONTRACT_NAME = "ops.p5.provenance.v1"
PROVENANCE_CONTRACT_VERSION = "1.0"
ISSUER = "private_companion"
P3_CONTRACT_NAME = "ops.context_orchestration.v1"
P3_CONTRACT_VERSION = "1.0"
P3_CONTRACT_FINGERPRINT = "3bb7a12af05bb4d9a47cef9f31e78752cec512b90b1d50ae49feef54470974f8"

TRUST_LEVEL_VALUES = ("T0", "T1", "T2", "T3", "T4")
SOURCE_KIND_VALUES = (
    "policy_config",
    "verified_authorization",
    "current_user_intent",
    "forwarded_text",
    "quoted_text",
    "vision_summary",
    "tool_output",
    "web_extract",
    "memory_recall",
    "derived_summary",
    "legacy_memory",
    "unknown",
)
FIREWALL_STATUS_VALUES = (
    "allowed",
    "sanitized",
    "blocked",
    "rejected",
    "quarantined",
    "unavailable",
    "unknown",
)
DISPOSITION_VALUES = ("allow", "shadow_quarantine", "deny_high_risk")
TRUST_LEVELS = frozenset(TRUST_LEVEL_VALUES)
SOURCE_KINDS = frozenset(SOURCE_KIND_VALUES)
FIREWALL_STATUSES = frozenset(FIREWALL_STATUS_VALUES)
DISPOSITIONS = frozenset(DISPOSITION_VALUES)
SINKS = frozenset(
    {
        "prompt_context",
        "prompt_forward_quote",
        "prompt_vision_summary",
        "memory_recall",
        "bridge_serialize",
        "bridge_serialization",
        "tool_execution",
        "tool_retrieval",
        "external_export",
        "cross_user_read",
    }
)
REASON_CODES = frozenset(
    {
        "p5_attested",
        "p5b_attested",
        "p5_nonexecuting",
        "evidence_only_nonexecuting",
        "untrusted_source_shadowed",
        "sanitized_source_shadowed",
        "upstream_security_denied",
        "upstream_security_shadowed",
        "high_risk_sink_denied",
        "invalid_segment",
        "invalid_source_shadowed",
        "legacy_unresolved",
        "owner_recovered",
        "derived_source",
    }
)
_SOURCE_TRUST = {
    "policy_config": "T0",
    "verified_authorization": "T1",
    "current_user_intent": "T2",
    "forwarded_text": "T3",
    "quoted_text": "T3",
    "vision_summary": "T3",
    "tool_output": "T3",
    "web_extract": "T3",
    "memory_recall": "T4",
    "derived_summary": "T4",
    "legacy_memory": "T4",
    "unknown": "T4",
}
DEFAULT_TTL_SECONDS = 30.0
MAX_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 1024
MAX_SINKS_PER_HANDLE = 8
_HEX = frozenset("0123456789abcdef")
_HANDLE_CREATION_TOKEN = object()


def provenance_contract_descriptor() -> dict[str, object]:
    return {
        "contract_name": PROVENANCE_CONTRACT_NAME,
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "record_fields": [
            "contract_name",
            "contract_version",
            "contract_fingerprint",
            "memory_id",
            "source_kind",
            "source_trust",
            "firewall_status",
            "source_event_ref_hash",
            "authority_attestation_ref_hash",
            "provenance_state",
            "migration_operation_ref",
            "recovery_operation_ref",
            "record_revision",
        ],
        "provenance_states": ["observed", "legacy_unresolved", "owner_recovered", "invalid"],
        "trust_levels": list(TRUST_LEVEL_VALUES),
        "source_kinds": list(SOURCE_KIND_VALUES),
        "firewall_statuses": list(FIREWALL_STATUS_VALUES),
        "dispositions": list(DISPOSITION_VALUES),
        "hash_format": "sha256_hex_lower",
        "attestation_issuer": ISSUER,
        "p3_contract_name": P3_CONTRACT_NAME,
        "p3_contract_version": P3_CONTRACT_VERSION,
    }


def provenance_contract_fingerprint() -> str:
    canonical = json.dumps(
        provenance_contract_descriptor(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


PROVENANCE_CONTRACT_FINGERPRINT = provenance_contract_fingerprint()
CONTRACT_FINGERPRINT = PROVENANCE_CONTRACT_FINGERPRINT


class P5AttestationError(ValueError):
    """Raised when metadata is outside the closed attestation contract."""


class P5AttestationHandle:
    __slots__ = ("__weakref__",)

    def __init__(self, creation_token: object | None = None) -> None:
        if creation_token is not _HANDLE_CREATION_TOKEN:
            raise TypeError("attestation handles are minted only by the registry")

    def __reduce__(self) -> object:
        raise TypeError("attestation handles cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("attestation handles cannot be serialized")

    def __getstate__(self) -> object:
        raise TypeError("attestation handles cannot be serialized")

    def __repr__(self) -> str:
        return "<P5AttestationHandle opaque>"

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("attestation handles cannot be subclassed")


@dataclass(frozen=True, slots=True)
class P5AttestationSnapshot:
    schema_version: str
    contract_name: str
    contract_version: str
    contract_fingerprint: str
    issuer: str
    issuer_epoch: str
    authority_attestation_ref_hash: str
    request_hash: str
    session_hash: str
    source_kind: str
    source_trust: str
    firewall_status: str
    disposition: str
    reason_codes: tuple[str, ...]
    source_event_ref_hash: str
    derived_from_ref_hash: str
    provenance_state: str
    p3_contract_name: str
    p3_contract_version: str
    p3_contract_fingerprint: str
    sink: str


@dataclass(slots=True)
class _Entry:
    request: object
    event: object
    p3_state: object
    request_hash: str
    session_hash: str
    source_kind: str
    source_trust: str
    firewall_status: str
    disposition: str
    reason_codes: tuple[str, ...]
    source_event_ref_hash: str
    derived_from_ref_hash: str
    authority_attestation_ref_hash: str
    issuer_epoch: str
    expires_at: float
    sinks: frozenset[str]


class P5AttestationRegistry:
    """Bounded, thread-safe registry with atomic one-shot consumption."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        default_ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._clock = clock or time.monotonic
        self._default_ttl_seconds = _validate_ttl(default_ttl_seconds)
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise P5AttestationError("registry capacity is invalid")
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._entries: dict[P5AttestationHandle, _Entry] = {}
        self._issuer_epoch = _new_hash()
        self._unloaded = False

    @property
    def issuer_epoch(self) -> str:
        with self._lock:
            return self._issuer_epoch

    def mint(
        self,
        request: object,
        event: object,
        p3_state: object,
        *,
        request_hash: str,
        session_hash: str,
        source_kind: str,
        source_trust: str,
        firewall_status: str,
        disposition: str,
        reason_codes: Iterable[str],
        source_event_ref_hash: str,
        sinks: Iterable[str],
        derived_from_ref_hash: str = "",
        ttl_seconds: float | None = None,
    ) -> P5AttestationHandle | None:
        _validate_anchor(request)
        _validate_anchor(event)
        _validate_anchor(p3_state)
        request_hash = _validate_hash(request_hash)
        session_hash = _validate_hash(session_hash)
        source_kind = _validate_enum(source_kind, SOURCE_KINDS, "source kind")
        source_trust = _validate_enum(source_trust, TRUST_LEVELS, "source trust")
        if _SOURCE_TRUST[source_kind] != source_trust:
            raise P5AttestationError("source kind and trust do not match")
        firewall_status = _validate_enum(firewall_status, FIREWALL_STATUSES, "firewall status")
        disposition = _validate_enum(disposition, DISPOSITIONS, "disposition")
        if source_trust in {"T3", "T4"} and disposition == "allow":
            raise P5AttestationError("untrusted source cannot be marked allowed")
        reasons = _validate_reason_codes(reason_codes)
        source_event_ref_hash = _validate_hash(source_event_ref_hash)
        if derived_from_ref_hash:
            derived_from_ref_hash = _validate_hash(derived_from_ref_hash)
        sink_values = _validate_sinks(sinks)
        ttl = self._default_ttl_seconds if ttl_seconds is None else _validate_ttl(ttl_seconds)

        with self._lock:
            if self._unloaded:
                return None
            self._cleanup_locked(self._clock())
            if len(self._entries) >= self._max_entries:
                return None
            handle = P5AttestationHandle(_HANDLE_CREATION_TOKEN)
            self._entries[handle] = _Entry(
                request=request,
                event=event,
                p3_state=p3_state,
                request_hash=request_hash,
                session_hash=session_hash,
                source_kind=source_kind,
                source_trust=source_trust,
                firewall_status=firewall_status,
                disposition=disposition,
                reason_codes=reasons,
                source_event_ref_hash=source_event_ref_hash,
                derived_from_ref_hash=derived_from_ref_hash,
                authority_attestation_ref_hash=_new_hash(),
                issuer_epoch=self._issuer_epoch,
                expires_at=self._clock() + ttl,
                sinks=sink_values,
            )
            return handle

    def consume(
        self,
        handle: object,
        request: object,
        event: object,
        p3_state: object,
        sink: object,
    ) -> P5AttestationSnapshot | None:
        if type(handle) is not P5AttestationHandle:
            return None
        with self._lock:
            entry = self._entries.pop(handle, None)
            if entry is None or self._unloaded:
                return None
            if self._clock() >= entry.expires_at or entry.issuer_epoch != self._issuer_epoch:
                return None
            if request is not entry.request or event is not entry.event or p3_state is not entry.p3_state:
                return None
            if not isinstance(sink, str) or sink not in entry.sinks:
                return None
            return P5AttestationSnapshot(
                schema_version=ATTESTATION_SCHEMA_VERSION,
                contract_name=PROVENANCE_CONTRACT_NAME,
                contract_version=PROVENANCE_CONTRACT_VERSION,
                contract_fingerprint=PROVENANCE_CONTRACT_FINGERPRINT,
                issuer=ISSUER,
                issuer_epoch=entry.issuer_epoch,
                authority_attestation_ref_hash=entry.authority_attestation_ref_hash,
                request_hash=entry.request_hash,
                session_hash=entry.session_hash,
                source_kind=entry.source_kind,
                source_trust=entry.source_trust,
                firewall_status=entry.firewall_status,
                disposition=entry.disposition,
                reason_codes=entry.reason_codes,
                source_event_ref_hash=entry.source_event_ref_hash,
                derived_from_ref_hash=entry.derived_from_ref_hash,
                provenance_state="observed",
                p3_contract_name=P3_CONTRACT_NAME,
                p3_contract_version=P3_CONTRACT_VERSION,
                p3_contract_fingerprint=P3_CONTRACT_FINGERPRINT,
                sink=sink,
            )

    def cleanup(self) -> int:
        with self._lock:
            return self._cleanup_locked(self._clock())

    def reset_epoch(self) -> str:
        with self._lock:
            self._entries.clear()
            self._issuer_epoch = _new_hash()
            return self._issuer_epoch

    def unload(self) -> None:
        with self._lock:
            self._entries.clear()
            self._issuer_epoch = _new_hash()
            self._unloaded = True

    def pending_count(self) -> int:
        with self._lock:
            self._cleanup_locked(self._clock())
            return len(self._entries)

    def _cleanup_locked(self, now: float) -> int:
        expired = [handle for handle, entry in self._entries.items() if now >= entry.expires_at]
        for handle in expired:
            self._entries.pop(handle, None)
        return len(expired)


def _new_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _validate_anchor(value: object) -> None:
    if value is None:
        raise P5AttestationError("identity anchor is missing")


def _validate_hash(value: object) -> str:
    if not isinstance(value, str):
        raise P5AttestationError("hash is invalid")
    value = value[7:] if value.startswith("sha256:") else value
    if len(value) != 64 or any(char not in _HEX for char in value):
        raise P5AttestationError("hash is invalid")
    return value


def _validate_enum(value: object, permitted: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in permitted:
        raise P5AttestationError(f"{label} is invalid")
    return value


def _validate_reason_codes(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise P5AttestationError("reason codes are invalid")
    try:
        normalized = tuple(values)
    except TypeError as exc:
        raise P5AttestationError("reason codes are invalid") from exc
    if not normalized or len(normalized) > len(REASON_CODES):
        raise P5AttestationError("reason codes are invalid")
    if len(normalized) != len(set(normalized)) or any(code not in REASON_CODES for code in normalized):
        raise P5AttestationError("reason codes are invalid")
    return tuple(sorted(normalized))


def _validate_sinks(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise P5AttestationError("sinks are invalid")
    try:
        normalized = tuple(values)
    except TypeError as exc:
        raise P5AttestationError("sinks are invalid") from exc
    if not normalized or len(normalized) > MAX_SINKS_PER_HANDLE:
        raise P5AttestationError("sinks are invalid")
    if len(normalized) != len(set(normalized)) or any(sink not in SINKS for sink in normalized):
        raise P5AttestationError("sinks are invalid")
    return frozenset(normalized)


def _validate_ttl(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise P5AttestationError("ttl is invalid")
    result = float(value)
    if not (0.0 < result <= MAX_TTL_SECONDS):
        raise P5AttestationError("ttl is invalid")
    return result


AttestationError = P5AttestationError
AttestationHandle = P5AttestationHandle
OpaqueAttestationHandle = P5AttestationHandle
AttestationSnapshot = P5AttestationSnapshot
AttestationRegistry = P5AttestationRegistry

__all__ = [
    "ATTESTATION_SCHEMA_VERSION",
    "AttestationError",
    "AttestationHandle",
    "AttestationRegistry",
    "AttestationSnapshot",
    "CONTRACT_FINGERPRINT",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "DISPOSITIONS",
    "FIREWALL_STATUSES",
    "ISSUER",
    "OpaqueAttestationHandle",
    "P3_CONTRACT_FINGERPRINT",
    "P3_CONTRACT_NAME",
    "P3_CONTRACT_VERSION",
    "P5AttestationError",
    "P5AttestationHandle",
    "P5AttestationRegistry",
    "P5AttestationSnapshot",
    "PROVENANCE_CONTRACT_FINGERPRINT",
    "PROVENANCE_CONTRACT_NAME",
    "PROVENANCE_CONTRACT_VERSION",
    "REASON_CODES",
    "SINKS",
    "SOURCE_KINDS",
    "TRUST_LEVELS",
    "provenance_contract_descriptor",
    "provenance_contract_fingerprint",
]
