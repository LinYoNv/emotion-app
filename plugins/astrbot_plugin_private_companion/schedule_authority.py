# -*- coding: utf-8 -*-
"""可信课程、班表、预约和用户确认适配器。

LLM 或普通聊天文本不能直接签发 ``source_refs``。适配器只接受结构化输入，
校验主体、时区、绝对时间和版本，并以幂等方式保留最新有效引用。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, asdict
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo


AUTHORITIES = {"calendar", "timetable", "roster", "appointment", "user_confirmation"}
VALID_STATES = {"active", "cancelled", "revoked", "superseded"}
VERIFICATION_STATES = {"valid", "expired", "cancelled", "revoked", "invalid"}


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _tz(value: Any) -> ZoneInfo | None:
    name = _text(value, 80)
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _moment(value: Any, timezone_name: str) -> datetime | None:
    if isinstance(value, datetime):
        current = value
    else:
        text = _text(value, 96)
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            current = datetime.fromisoformat(text)
        except ValueError:
            return None
    timezone = _tz(timezone_name)
    if current.tzinfo is None:
        if timezone is None:
            return None
        return current.replace(tzinfo=timezone)
    return current.astimezone(timezone or current.tzinfo)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _ref_id(
    namespace: str,
    event_id: str,
    revision: str,
    provider: str = "",
    subject_actor_id: str = "",
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


def _explicit_false(value: Any) -> bool:
    if value is False or value == 0:
        return True
    return _text(value, 16).lower() in {"false", "no", "denied", "invalid", "revoked"}


def _revision_key(value: Any) -> tuple[Any, ...]:
    text = _text(value, 80)
    parts = []
    for part in re.split(r"([0-9]+)", text):
        if part.isdigit():
            parts.append((1, int(part)))
        elif part:
            parts.append((0, part.lower()))
    return tuple(parts)


@dataclass(frozen=True)
class TrustedScheduleRef:
    namespace: str
    event_id: str
    provider: str
    revision: str
    updated_at: str
    timezone: str
    subject_actor_id: str
    effective_from: str
    effective_to: str
    recurrence: Any = None
    state: str = "active"
    issued_at: str = ""
    expires_at: str = ""
    authority_kind: str = "calendar"
    confirmation_event_id: str = ""
    confirmation_actor_id: str = ""
    proposition: str = ""
    confirmed_at: str = ""
    revocation_of: str = ""
    supersedes_ref_id: str = ""
    revocation_reason: str = ""
    target_user_id: str = ""
    authorized: bool = True
    permission_valid: bool = True
    subject_authorized: bool = True
    ref_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_plan_fields(self) -> dict[str, Any]:
        """Return the minimal canonical fields accepted by the C3 write gate."""

        return {
            "source_refs": [
                self.ref_id
                or _ref_id(
                    self.namespace,
                    self.event_id,
                    self.revision,
                    self.provider,
                    self.subject_actor_id,
                    updated_at=self.updated_at,
                    timezone=self.timezone,
                    effective_from=self.effective_from,
                    effective_to=self.effective_to,
                    expires_at=self.expires_at,
                    authority_kind=self.authority_kind,
                    confirmation_event_id=self.confirmation_event_id,
                    confirmation_actor_id=self.confirmation_actor_id,
                    proposition=self.proposition,
                    confirmed_at=self.confirmed_at,
                    target_user_id=self.target_user_id,
                )
            ],
            "source_refs_trusted": True,
            "trusted_source_refs": True,
            "authority_kind": self.authority_kind,
            "commitment_level": "confirmed",
            "subject_actor_id": self.subject_actor_id,
            "actor_type": "bot",
            "schedule_ref": self.as_dict(),
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


@dataclass(frozen=True)
class RejectedSource:
    ok: bool = False
    reason: str = "invalid_source"
    decision_trace: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "decision_trace": list(self.decision_trace)}

    def __bool__(self) -> bool:
        return False


class VerificationResult(str):
    def __new__(cls, status: str, ref: TrustedScheduleRef | None = None):
        obj = str.__new__(cls, status)
        obj.status = status
        obj.ref = ref
        return obj


class ScheduleAuthorityAdapter:
    """签发、验证、撤销和改期可信日程引用。"""

    def __init__(self, *, namespace: str = "private_companion", provider: str = "local", clock: Any = None) -> None:
        self.namespace = _text(namespace, 120) or "private_companion"
        self.provider = _text(provider, 120) or "local"
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._refs: dict[tuple[str, str, str], TrustedScheduleRef] = {}
        self._latest: dict[tuple[str, str], str] = {}

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            value = datetime.now().astimezone()
        return value if value.tzinfo else value.astimezone()

    def issue_or_update(
        self,
        raw_event: dict[str, Any],
        subject_actor_id: str,
        target_user_id: str = "",
    ) -> TrustedScheduleRef | RejectedSource:
        if not isinstance(raw_event, dict):
            return RejectedSource(reason="event_not_object")
        subject = _text(subject_actor_id, 120)
        if not subject:
            return RejectedSource(reason="subject_actor_id_required")
        declared_subject = _text(raw_event.get("subject_actor_id"), 120)
        if declared_subject and declared_subject != subject:
            return RejectedSource(reason="subject_binding_conflict")
        authority = _text(raw_event.get("authority_kind") or raw_event.get("authority"), 48).lower()
        if authority not in AUTHORITIES:
            return RejectedSource(reason="authority_not_supported")
        namespace = _text(raw_event.get("namespace"), 120) or self.namespace
        event_id = _text(raw_event.get("event_id") or raw_event.get("id"), 160)
        provider = _text(raw_event.get("provider"), 120) or self.provider
        revision = _text(raw_event.get("revision"), 80)
        timezone_name = _text(raw_event.get("timezone"), 80)
        if not event_id or not provider or not revision or not timezone_name:
            return RejectedSource(reason="namespace_event_provider_revision_timezone_required")
        if any(
            _explicit_false(raw_event.get(key))
            for key in ("authorized", "permission_valid", "subject_authorized")
            if key in raw_event
        ):
            return RejectedSource(reason="permission_denied")
        confirmation_event_id = _text(raw_event.get("confirmation_event_id"), 160)
        confirmation_actor_id = _text(
            raw_event.get("confirmation_actor_id")
            or raw_event.get("confirmed_by")
            or raw_event.get("confirmer_id")
            or raw_event.get("source_actor_id"),
            120,
        )
        proposition = _text(
            raw_event.get("proposition")
            or raw_event.get("title")
            or raw_event.get("activity")
            or raw_event.get("summary"),
            240,
        )
        confirmed_at_raw = raw_event.get("confirmed_at") or raw_event.get("confirmation_time")
        confirmed_at = _moment(confirmed_at_raw, timezone_name) if confirmed_at_raw else None
        if authority == "user_confirmation" and (
            not confirmation_event_id
            or not confirmation_actor_id
            or confirmation_actor_id == subject
            or not proposition
            or not confirmed_at
        ):
            return RejectedSource(reason="structured_confirmation_required")
        timezone = _tz(timezone_name)
        if timezone is None:
            return RejectedSource(reason="invalid_timezone")
        start = _moment(raw_event.get("effective_from") or raw_event.get("start_at") or raw_event.get("start"), timezone_name)
        end = _moment(raw_event.get("effective_to") or raw_event.get("end_at") or raw_event.get("end"), timezone_name)
        if start is None or end is None or end <= start:
            return RejectedSource(reason="absolute_effective_interval_required")
        state = _text(raw_event.get("state"), 24).lower() or "active"
        if state not in VALID_STATES:
            return RejectedSource(reason="invalid_state")
        key = (namespace, event_id, revision)
        existing = self._refs.get(key)
        if existing is not None:
            if existing.provider != provider or existing.subject_actor_id != subject:
                return RejectedSource(reason="revision_binding_conflict")
            return existing
        now = self._now()
        updated_raw = raw_event.get("updated_at")
        if updated_raw in (None, ""):
            return RejectedSource(reason="updated_at_required")
        updated = _moment(updated_raw, timezone_name)
        if updated is None:
            return RejectedSource(reason="invalid_updated_at")
        expires_raw = raw_event.get("expires_at")
        expires = _moment(expires_raw, timezone_name) if expires_raw else None
        if expires_raw and expires is None:
            return RejectedSource(reason="invalid_expires_at")
        ref_id = _ref_id(
            namespace,
            event_id,
            revision,
            provider,
            subject,
            updated_at=_iso(updated),
            timezone=timezone_name,
            effective_from=_iso(start),
            effective_to=_iso(end),
            expires_at=_iso(expires) if expires else "",
            authority_kind=authority,
            confirmation_event_id=confirmation_event_id,
            confirmation_actor_id=confirmation_actor_id,
            proposition=proposition,
            confirmed_at=_iso(confirmed_at) if confirmed_at else "",
            target_user_id=_text(target_user_id or raw_event.get("target_user_id"), 120),
        )
        ref = TrustedScheduleRef(
            namespace=namespace,
            event_id=event_id,
            provider=provider,
            revision=revision,
            updated_at=_iso(updated),
            timezone=timezone_name,
            subject_actor_id=subject,
            effective_from=_iso(start),
            effective_to=_iso(end),
            recurrence=deepcopy(raw_event.get("recurrence")),
            state=state,
            issued_at=_iso(now),
            expires_at=_iso(expires) if expires else "",
            authority_kind=authority,
            confirmation_event_id=confirmation_event_id,
            confirmation_actor_id=confirmation_actor_id,
            proposition=proposition,
            confirmed_at=_iso(confirmed_at) if confirmed_at else "",
            revocation_of=_text(raw_event.get("revocation_of") or raw_event.get("revokes_event_id"), 160),
            supersedes_ref_id=_text(raw_event.get("supersedes_ref_id"), 160),
            revocation_reason=_text(raw_event.get("revocation_reason") or raw_event.get("reason"), 240),
            target_user_id=_text(target_user_id or raw_event.get("target_user_id"), 120),
            authorized=not _explicit_false(raw_event.get("authorized")),
            permission_valid=not _explicit_false(raw_event.get("permission_valid")),
            subject_authorized=not _explicit_false(raw_event.get("subject_authorized")),
            ref_id=ref_id,
        )
        latest_key = (namespace, event_id)
        old_revision = self._latest.get(latest_key)
        if old_revision and old_revision != revision:
            old = self._refs.get((namespace, event_id, old_revision))
            if old is not None and _revision_key(revision) < _revision_key(old_revision):
                ref = TrustedScheduleRef(**{**ref.as_dict(), "state": "superseded"})
                self._refs[key] = ref
                return ref
            if old is not None and old.state == "active":
                self._refs[(namespace, event_id, old_revision)] = TrustedScheduleRef(
                    **{**old.as_dict(), "state": "superseded"}
                )
        self._refs[key] = ref
        self._latest[latest_key] = revision
        return ref

    def verify(self, ref: TrustedScheduleRef | dict[str, Any] | str, now: datetime | None = None) -> VerificationResult:
        candidate: TrustedScheduleRef | None
        if isinstance(ref, TrustedScheduleRef):
            candidate = ref
        elif isinstance(ref, dict):
            try:
                candidate = TrustedScheduleRef(**{key: ref[key] for key in TrustedScheduleRef.__dataclass_fields__ if key in ref})
            except Exception:
                candidate = None
        else:
            value = _text(ref, 160)
            candidate = next((item for item in self._refs.values() if item.ref_id == value), None)
        if candidate is None:
            return VerificationResult("invalid")
        expected_ref_id = _ref_id(
            candidate.namespace,
            candidate.event_id,
            candidate.revision,
            candidate.provider,
            candidate.subject_actor_id,
            updated_at=candidate.updated_at,
            timezone=candidate.timezone,
            effective_from=candidate.effective_from,
            effective_to=candidate.effective_to,
            expires_at=candidate.expires_at,
            authority_kind=candidate.authority_kind,
            confirmation_event_id=candidate.confirmation_event_id,
            confirmation_actor_id=candidate.confirmation_actor_id,
            proposition=candidate.proposition,
            confirmed_at=candidate.confirmed_at,
            target_user_id=candidate.target_user_id,
        )
        # ``ref_id`` is the adapter's stable binding between the structured
        # event and its source reference.  A copied or hand-built dataclass
        # with a missing/mismatched ID is not a trusted source, even if its
        # interval and state fields happen to look valid.
        if not candidate.ref_id or candidate.ref_id != expected_ref_id:
            return VerificationResult("invalid", candidate)
        if (
            not candidate.namespace
            or not candidate.event_id
            or not candidate.provider
            or not candidate.revision
            or not candidate.updated_at
            or not candidate.timezone
            or not candidate.subject_actor_id
            or candidate.authority_kind not in AUTHORITIES
            or _tz(candidate.timezone) is None
            or _moment(candidate.updated_at, candidate.timezone) is None
            or _explicit_false(candidate.authorized)
            or _explicit_false(candidate.permission_valid)
            or _explicit_false(candidate.subject_authorized)
        ):
            return VerificationResult("invalid", candidate)
        # Always verify against the adapter's latest stored revision when the
        # reference belongs to this adapter.  A caller may retain an old
        # immutable dataclass after a reschedule; trusting that stale object
        # would let a superseded commitment re-enter future views.
        stored = self._refs.get((candidate.namespace, candidate.event_id, candidate.revision))
        if stored is not None:
            if candidate.ref_id and stored.ref_id and candidate.ref_id != stored.ref_id:
                return VerificationResult("invalid", stored)
            candidate = stored
        state = _text(candidate.state, 24).lower()
        latest_revision = self._latest.get((candidate.namespace, candidate.event_id))
        if latest_revision and latest_revision != candidate.revision and state == "active":
            return VerificationResult("revoked", candidate)
        if state == "cancelled":
            return VerificationResult("cancelled", candidate)
        if state in {"revoked", "superseded"}:
            return VerificationResult("revoked", candidate)
        current = now or self._now()
        if current.tzinfo is None:
            current = current.astimezone()
        start = _moment(candidate.effective_from, candidate.timezone)
        end = _moment(candidate.effective_to, candidate.timezone)
        if start is None or end is None or end <= start:
            return VerificationResult("invalid", candidate)
        expires = _moment(candidate.expires_at, candidate.timezone) if candidate.expires_at else None
        if candidate.expires_at and expires is None:
            return VerificationResult("invalid", candidate)
        if expires is not None and current >= expires:
            return VerificationResult("expired", candidate)
        return VerificationResult("valid", candidate)

    def revoke_or_reschedule(
        self,
        event_id: str,
        revision: str,
        reason: str,
        effective_at: datetime | str | None = None,
        *,
        reschedule: dict[str, Any] | None = None,
        subject_actor_id: str = "",
    ) -> TrustedScheduleRef | RejectedSource:
        event = _text(event_id, 160)
        old_revision = _text(revision, 80)
        key = (self.namespace, event, old_revision)
        old = self._refs.get(key)
        if old is None:
            return RejectedSource(reason="source_not_found")
        self._refs[key] = TrustedScheduleRef(**{**old.as_dict(), "state": "superseded"})
        payload = deepcopy(reschedule or {})
        payload.setdefault("event_id", event)
        payload.setdefault("provider", old.provider)
        payload.setdefault("namespace", old.namespace)
        payload.setdefault("timezone", old.timezone)
        payload.setdefault("authority_kind", old.authority_kind)
        payload.setdefault("subject_actor_id", subject_actor_id or old.subject_actor_id)
        payload.setdefault("effective_from", old.effective_from)
        payload.setdefault("effective_to", old.effective_to)
        payload.setdefault("target_user_id", old.target_user_id)
        payload.setdefault("confirmation_event_id", old.confirmation_event_id)
        payload.setdefault("confirmation_actor_id", old.confirmation_actor_id)
        payload.setdefault("proposition", old.proposition)
        payload.setdefault("confirmed_at", old.confirmed_at)
        payload.setdefault("supersedes_ref_id", old.ref_id)
        payload.setdefault("revocation_of", old.ref_id)
        payload.setdefault("revocation_reason", _text(reason, 240))
        payload.setdefault("revision", f"{old_revision}-next" if reschedule else f"{old_revision}-cancelled")
        payload.setdefault("state", "active" if reschedule else "cancelled")
        effective_value = (
            effective_at.isoformat()
            if isinstance(effective_at, datetime)
            else _text(effective_at, 96)
            if effective_at is not None
            else _iso(self._now())
        )
        payload.setdefault("updated_at", effective_value)
        return self.issue_or_update(payload, payload["subject_actor_id"])

    def snapshot(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self._refs.values()]


def validate_structured_schedule_ref(
    ref: TrustedScheduleRef | Mapping[str, Any] | Any,
    *,
    source_refs: Any = None,
    expected_authority: str = "",
    expected_subject: str = "",
    expected_target: str = "",
    now: datetime | None = None,
    adapter: ScheduleAuthorityAdapter | None = None,
) -> tuple[str, str]:
    """Validate an adapter-issued schedule reference at every trust boundary.

    The legacy ``source_refs_trusted``/``authority_verified`` booleans remain
    useful for diagnostics, but they are not evidence.  A hard schedule
    commitment needs a complete structured reference, its stable ``ref_id``
    in ``source_refs``, a matching actor/authority binding, and a valid
    adapter signature.  The returned status is one of ``valid``, ``expired``,
    ``cancelled``, ``revoked`` or ``invalid``.
    """

    if isinstance(ref, TrustedScheduleRef):
        candidate = ref.as_dict()
    elif isinstance(ref, Mapping):
        candidate = dict(ref)
    else:
        return "invalid", "missing_schedule_ref"

    def _refs(value: Any) -> set[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return set()
        return {_text(item, 240) for item in value if _text(item, 240)}

    ref_id = _text(candidate.get("ref_id"), 240)
    if not ref_id or ref_id not in _refs(source_refs):
        return "invalid", "schedule_ref_not_in_source_refs"

    required = (
        "namespace",
        "event_id",
        "provider",
        "revision",
        "updated_at",
        "timezone",
        "subject_actor_id",
        "effective_from",
        "effective_to",
        "state",
    )
    missing = [key for key in required if not _text(candidate.get(key), 240)]
    if missing:
        return "invalid", "schedule_ref_incomplete"

    timezone_name = _text(candidate.get("timezone"), 80)
    timezone = _tz(timezone_name)
    if timezone is None:
        return "invalid", "invalid_timezone"
    updated = _moment(candidate.get("updated_at"), timezone_name)
    start = _moment(candidate.get("effective_from"), timezone_name)
    end = _moment(candidate.get("effective_to"), timezone_name)
    if updated is None or start is None or end is None or end <= start:
        return "invalid", "absolute_effective_interval_required"
    expires_raw = candidate.get("expires_at")
    expires = _moment(expires_raw, timezone_name) if expires_raw not in (None, "") else None
    if expires_raw not in (None, "") and expires is None:
        return "invalid", "invalid_expires_at"

    authority = _text(candidate.get("authority_kind"), 48).lower()
    if authority not in AUTHORITIES:
        return "invalid", "schedule_authority_invalid"
    if expected_authority and authority != _text(expected_authority, 48).lower():
        return "invalid", "schedule_authority_mismatch"
    subject = _text(candidate.get("subject_actor_id"), 120)
    if not _text(expected_subject, 120):
        return "invalid", "schedule_subject_required"
    if expected_subject and subject != _text(expected_subject, 120):
        return "invalid", "schedule_subject_mismatch"
    target = _text(candidate.get("target_user_id"), 120)
    if expected_target and target != _text(expected_target, 120):
        return "invalid", "schedule_target_mismatch"
    if any(_explicit_false(candidate.get(key)) for key in ("authorized", "permission_valid", "subject_authorized")):
        return "invalid", "permission_denied"

    state = _text(candidate.get("state"), 24).lower()
    if state not in VALID_STATES:
        return "invalid", "invalid_state"

    expected_ref_id = _ref_id(
        _text(candidate.get("namespace"), 120),
        _text(candidate.get("event_id"), 160),
        _text(candidate.get("revision"), 80),
        _text(candidate.get("provider"), 120),
        subject,
        updated_at=_iso(updated),
        timezone=timezone_name,
        effective_from=_iso(start),
        effective_to=_iso(end),
        expires_at=_iso(expires) if expires else "",
        authority_kind=authority,
        confirmation_event_id=_text(candidate.get("confirmation_event_id"), 160),
        confirmation_actor_id=_text(candidate.get("confirmation_actor_id"), 120),
        proposition=_text(candidate.get("proposition"), 240),
        confirmed_at=_text(candidate.get("confirmed_at"), 96),
        target_user_id=target,
    )
    if ref_id != expected_ref_id:
        return "invalid", "schedule_ref_id_mismatch"

    if authority == "user_confirmation":
        confirmer = _text(candidate.get("confirmation_actor_id"), 120)
        proposition = _text(candidate.get("proposition"), 240)
        confirmed_at = _text(candidate.get("confirmed_at"), 96)
        if (
            not _text(candidate.get("confirmation_event_id"), 160)
            or not confirmer
            or confirmer == subject
            or not proposition
            or not confirmed_at
            or _moment(confirmed_at, _text(candidate.get("timezone"), 80)) is None
        ):
            return "invalid", "structured_confirmation_required"

    verifier = adapter or ScheduleAuthorityAdapter(clock=lambda: now or datetime.now().astimezone())
    try:
        result = verifier.verify(candidate, now=now)
        status = _text(getattr(result, "status", result), 24).lower()
    except Exception:
        return "invalid", "schedule_ref_verification_error"
    if status not in VERIFICATION_STATES:
        return "invalid", "schedule_ref_verification_error"
    if status == "valid":
        return status, ""
    reason = {
        "expired": "expired",
        "cancelled": "status_cancelled",
        "revoked": "status_revoked",
        "invalid": "invalid_schedule_ref",
    }[status]
    return status, reason


__all__ = [
    "ScheduleAuthorityAdapter",
    "TrustedScheduleRef",
    "RejectedSource",
    "VerificationResult",
    "validate_structured_schedule_ref",
]
