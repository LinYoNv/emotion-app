# -*- coding: utf-8 -*-
"""Low-noise, local activity capture for chat-side C3 agenda data."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

try:
    from .agenda_contracts import normalize_observed_activity, stable_id
except ImportError:
    from agenda_contracts import normalize_observed_activity, stable_id


COLLABORATION_HINTS = {
    "代码", "插件", "开发", "排障", "测试", "部署", "运维", "项目", "创作", "一起", "继续",
    "code", "plugin", "debug", "test", "deploy", "project", "writing",
}


def classify_interaction(text: str, *, message_count: int = 1) -> str:
    content = str(text or "").strip()
    lowered = content.lower()
    if not content:
        return "ordinary"
    if any(token in content or token in lowered for token in COLLABORATION_HINTS) and len(content) >= 4:
        return "sustained" if message_count >= 2 else "candidate"
    if message_count >= 4 and len(content) >= 12:
        return "sustained"
    return "short" if len(content) > 12 else "ordinary"


class ActivityCapture:
    """Aggregate messages by conversation and time bucket before emitting facts."""

    def __init__(self, *, window_minutes: int = 30, min_sustained_messages: int = 3):
        self.window_minutes = max(5, int(window_minutes))
        self.min_sustained_messages = max(2, int(min_sustained_messages))
        self._buckets: dict[tuple[str, str], dict[str, Any]] = {}

    def _bucket_start(self, event_time: datetime) -> datetime:
        current = event_time.replace(second=0, microsecond=0)
        return current - timedelta(minutes=current.minute % self.window_minutes)

    def capture_message(
        self,
        *,
        text: str,
        event_time: datetime,
        source_ref: str,
        conversation_id: str,
        participant: str = "user",
        message_count: int = 1,
        topic: str = "",
        visibility: str = "private",
    ) -> dict[str, Any] | None:
        conversation = str(conversation_id or "").strip() or "unknown"
        bucket_start = self._bucket_start(event_time)
        key = (conversation, bucket_start.isoformat())
        bucket = self._buckets.setdefault(
            key,
            {
                "conversation_id": conversation,
                "bucket_start": bucket_start,
                "bucket_end": bucket_start + timedelta(minutes=self.window_minutes),
                "texts": [],
                "source_refs": [],
                "participants": [],
                "count": 0,
                "first_at": event_time,
                "last_at": event_time,
                "topic": str(topic or "").strip(),
                "visibility": str(visibility or "private"),
            },
        )
        ref = str(source_ref or "").strip()
        is_duplicate = bool(ref and ref in bucket["source_refs"])
        if not is_duplicate:
            bucket["count"] = max(int(bucket["count"]) + 1, int(message_count or 0), 1)
            if ref:
                bucket["source_refs"].append(ref)
            clean_text = " ".join(str(text or "").split())[:240]
            if clean_text:
                bucket["texts"].append(clean_text)
            person = str(participant or "user").strip()
            if person and person not in bucket["participants"]:
                bucket["participants"].append(person)
            bucket["first_at"] = min(bucket["first_at"], event_time)
            bucket["last_at"] = max(bucket["last_at"], event_time)
            if topic and not bucket["topic"]:
                bucket["topic"] = str(topic).strip()

        interaction = classify_interaction(text, message_count=int(bucket["count"]))
        if int(bucket["count"]) < self.min_sustained_messages:
            return None

        title = bucket["topic"] or (bucket["texts"][-1] if bucket["texts"] else "持续互动")
        activity_id = stable_id("activity", conversation, bucket_start.isoformat(), bucket["topic"] or "conversation")
        return normalize_observed_activity(
            {
                "activity_id": activity_id,
                "title": title,
                "summary": "；".join(bucket["texts"][-3:])[:360],
                "kind": "conversation_activity",
                "start_at": bucket["first_at"].isoformat(),
                "end_at": bucket["last_at"].isoformat(),
                "source": "conversation",
                "source_refs": list(bucket["source_refs"]),
                "participants": list(bucket["participants"]) + (["bot"] if "bot" not in bucket["participants"] else []),
                "evidence_level": "L2",
                "visibility": bucket["visibility"],
                "certainty": "high" if bucket["count"] >= self.min_sustained_messages + 1 else "medium",
                "status": "active",
                "actor_type": "bot",
                "subject_actor_id": "bot_self",
                "source_actor_id": str(participant or "user").strip() or "system",
            },
            now=event_time,
        )

    def capture_hard_fact(
        self,
        *,
        title: str,
        start_at: datetime,
        end_at: datetime | None,
        source: str,
        source_refs: list[str] | tuple[str, ...],
        participants: list[str] | None = None,
        kind: str = "tool_activity",
        visibility: str = "private",
        certainty: str = "high",
    ) -> dict[str, Any]:
        return normalize_observed_activity(
            {
                "activity_id": stable_id("activity", source, title, start_at.isoformat(), list(source_refs)),
                "title": title,
                "kind": kind,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat() if end_at else "",
                "source": source,
                "source_refs": list(source_refs),
                "participants": participants or ["bot"],
                "evidence_level": "L3",
                "visibility": visibility,
                "certainty": certainty,
                "status": "completed" if end_at else "active",
                "actor_type": "bot",
                "subject_actor_id": "bot_self",
                "source_actor_id": str(source or "system").strip() or "system",
            },
            now=start_at,
        )

    def capture_tool_activity(self, **kwargs: Any) -> dict[str, Any]:
        """Small named alias for callers that capture a tool-side hard fact."""

        return self.capture_hard_fact(**kwargs)
