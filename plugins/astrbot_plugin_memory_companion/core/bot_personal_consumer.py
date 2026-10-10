from __future__ import annotations

"""Failure-contained consumer for the optional Bot Personal Bridge."""

from copy import deepcopy
from typing import Any

from .bot_personal_dto import BotPersonalArchiveDTO, BotPersonalValidationError, build_bot_personal_archive


def _base(*, state: str = "retry", attempts: int = 0, error_code: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "record_id": "",
        "deduplicated": False,
        "version": 0,
        "error_code": error_code,
        "state": state,
        "attempts": max(0, int(attempts or 0)),
    }


class BotPersonalConsumer:
    def __init__(
        self,
        bridge: Any,
        *,
        max_attempts: int = 3,
        producer_capability: Any = None,
    ) -> None:
        self.bridge = bridge
        self.max_attempts = max(1, int(max_attempts or 3))
        self.producer_capability = producer_capability

    async def consume_bot_personal_archive(
        self,
        envelope: BotPersonalArchiveDTO | dict[str, Any],
        *,
        attempt: int = 1,
        max_attempts: int | None = None,
        producer_capability: Any = None,
    ) -> dict[str, Any]:
        attempts = max(1, int(attempt or 1))
        ceiling = max(1, int(max_attempts or self.max_attempts))
        try:
            dto = build_bot_personal_archive(envelope)
        except BotPersonalValidationError as exc:
            return {**_base(state="invalid", attempts=attempts, error_code=exc.error_code), "field": exc.field}
        try:
            sender = getattr(self.bridge, "record_bot_personal_archive", None)
        except Exception:
            sender = None
        if not callable(sender):
            return _base(state="dead_letter" if attempts >= ceiling else "retry", attempts=attempts, error_code="bridge_method_unavailable")
        try:
            authority = (
                producer_capability
                if producer_capability is not None
                else self.producer_capability
            )
            result = (
                await sender(dto, producer_capability=authority)
                if authority is not None
                else await sender(dto)
            )
        except Exception:
            return _base(state="dead_letter" if attempts >= ceiling else "retry", attempts=attempts, error_code="bridge_exception")
        if not isinstance(result, dict):
            return _base(state="dead_letter" if attempts >= ceiling else "retry", attempts=attempts, error_code="invalid_bridge_response")
        normalized = _base(
            state=str(result.get("state") or ("deduplicated" if result.get("deduplicated") else "sent" if result.get("ok") else "retry")),
            attempts=attempts,
            error_code=result.get("error_code"),
        )
        normalized.update({key: result[key] for key in ("record_id", "deduplicated", "version", "field") if key in result})
        normalized["ok"] = bool(result.get("ok"))
        if normalized["ok"]:
            normalized["state"] = "deduplicated" if normalized["deduplicated"] else "sent"
            return normalized
        if normalized["state"] not in {"invalid", "version_conflict", "stale_version", "dead_letter"}:
            normalized["state"] = "dead_letter" if attempts >= ceiling else "retry"
        return normalized

    async def consume(self, envelope: BotPersonalArchiveDTO | dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await self.consume_bot_personal_archive(envelope, **kwargs)


async def consume_bot_personal_archive(
    bridge: Any,
    envelope: BotPersonalArchiveDTO | dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    producer_capability: Any = None,
) -> dict[str, Any]:
    return await BotPersonalConsumer(
        bridge,
        max_attempts=max_attempts,
        producer_capability=producer_capability,
    ).consume_bot_personal_archive(
        deepcopy(envelope) if isinstance(envelope, dict) else envelope,
        attempt=attempt,
    )


__all__ = ["BotPersonalConsumer", "consume_bot_personal_archive"]
