"""Deterministic group-atmosphere extraction for the Companion persona.

The contract owns no persistence, clock, network, or platform access.  Runtime
adapters pass already-sanitised recent message snapshots and consume the mood
projection when injecting group context into a reply prompt, so the model can
match the group's current energy instead of guessing.

Mood labels are intentionally coarse: ``tease`` (调侃), ``banter`` (互怼),
``serious`` (严肃), ``tension`` (火药味), ``confession`` (告白), and
``dead_silence`` (冷场).
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping


GROUP_MOOD_VERSION = "group_mood.v1"
MOOD_LABELS = ("tease", "banter", "serious", "tension", "confession", "dead_silence")
MOOD_LABELS_ZH = {
    "tease": "调侃",
    "banter": "互怼",
    "serious": "严肃",
    "tension": "火药味",
    "confession": "告白",
    "dead_silence": "冷场",
}

# Frequency-weighted textual signals.  Each entry: (regex, mood, weight).
_MOOD_SIGNALS: tuple[tuple[str, str, float], ...] = (
    # 调侃: laughter, exaggeration, playful asides.
    (r"哈哈+|呵呵+|嘿嘿+|笑死|没绷住|绷不住|好笑|好乐|乐了|2333+|www+|笑死我了|xswl+", "tease", 1.6),
    (r"滑稽|阴阳|玩梗|整活|搞事情|搞事|皮一下|开玩笑的|我开个玩笑", "tease", 1.4),
    (r"修罗场|血流成河|没绷住|绷不住|乐子", "tease", 2.0),
    # 互怼: trash talk often follows an @ / reply with a light jab.
    (r"你不行|菜鸡|就这\??|拉胯|菜\b|菜到|太菜|你这不行|就这就这", "banter", 1.8),
    (r"？？？+", "banter", 0.6),
    # 严肃: going serious, stopping the joke.
    (r"说正事|认真讲|说真的|认真的|严肃点|不是玩笑|不开玩笑|别闹了|谈正事", "serious", 2.4),
    (r"认真点|正事|正经事|我是认真的", "serious", 2.0),
    # 火药味: escalation, confrontation.
    (r"你什么意思|你再说一遍|别太过分|太过了|滚\b|有病吧|闭嘴|欠揍|别吵|吵什么|急了|急了急了", "tension", 2.6),
    (r"你(是不是)?(就)?想打架|单挑|开喷|骂人|人身攻击|侮辱", "tension", 2.8),
    # 告白: explicit affection inside a group environment.
    (r"我喜欢你|喜欢你|表白|在一起吧|好喜欢|心动|好心动|暗恋", "confession", 2.6),
    (r"想要你|想你了|舍不得|抱抱你|贴贴", "confession", 1.4),
)

# Social tension aggregates confrontational and edge signals (0-100 scale).
_TENSION_WEIGHTS = {"tension": 1.0, "banter": 0.45, "tease": 0.18, "confession": 0.12, "serious": 0.1, "dead_silence": 0.0}

# Half-lives per mood: how long each atmosphere lingers without new signals.
_MOOD_HALF_LIVES = {
    "tease": 1500.0,
    "banter": 1800.0,
    "serious": 900.0,
    "tension": 7200.0,
    "confession": 2400.0,
    "dead_silence": 3600.0,
}


def _finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, Mapping):
        text = message.get("text") or message.get("content") or message.get("message")
        if text is not None:
            return str(text)
    return ""


def _message_key(message: Any, index: int) -> str:
    """Return a stable key so a sliding context window is only folded once."""
    if isinstance(message, Mapping):
        message_id = str(message.get("message_id") or message.get("id") or "").strip()
        if message_id:
            return f"id:{message_id}"
        ts = str(message.get("ts") or message.get("timestamp") or "").strip()
        sender = str(message.get("sender_id") or message.get("user_id") or "").strip()
        text = _message_text(message).strip()
        if ts or sender or text:
            return f"msg:{ts}|{sender}|{text}"
    return f"idx:{index}:{_message_text(message).strip()}"


def _decay(score: float, half_life: float, elapsed: float) -> float:
    if score <= 0:
        return 0.0
    if half_life <= 0:
        return 0.0
    return max(0.0, score * (0.5 ** (max(0.0, elapsed) / half_life)))


def project_group_mood(value: Any, *, now: Any) -> dict[str, Any]:
    """Project a persisted mood snapshot to the current time (decay included).

    Returns an empty mapping when the payload is not a valid group-mood
    snapshot, mirroring the interaction-dynamics contract so callers can
    distinguish "absent" from "relaxed".
    """
    raw = dict(value) if isinstance(value, Mapping) else {}
    if raw.get("version") != GROUP_MOOD_VERSION:
        return {}
    current_ts = max(0.0, _finite(now)) if now is not None else 0.0
    updated = max(0.0, _finite(raw.get("updated_at")))
    elapsed = max(0.0, current_ts - updated) if current_ts and updated else 0.0
    scores: dict[str, float] = {}
    stored = raw.get("mood_scores")
    if isinstance(stored, Mapping):
        for label in MOOD_LABELS:
            value = _finite(stored.get(label), 0.0)
            scores[label] = round(_decay(max(0.0, value), _MOOD_HALF_LIVES.get(label, 1800.0), elapsed), 2)
    tension = _finite(raw.get("social_tension"))
    if tension > 0:
        tension = round(_decay(min(100.0, tension), 2400.0, elapsed), 2)
    top_mood = _top_mood(scores)
    return {
        "version": GROUP_MOOD_VERSION,
        "mood_scores": scores,
        "top_mood": top_mood,
        "social_tension": tension,
        "updated_at": updated,
        "decayed_at": current_ts,
        "message_count": max(0, int(_finite(raw.get("message_count"), 0))),
        "processed_message_keys": [
            str(key) for key in (raw.get("processed_message_keys") or []) if str(key)
        ][-128:],
    }


def settle_group_mood(
    existing: Any,
    *,
    messages: Iterable[Any],
    now: Any,
    current_ts: float | None = None,
) -> dict[str, Any]:
    """Fold a batch of recent messages into the mood state.

    Time-based decay is applied first to the prior snapshot; new textual
    signals add to the surviving scores; the social-tension scalar is a
    weighted blend of the surviving tension and burst energy.
    """
    current_ts = max(0.0, _finite(now)) if now is not None else max(0.0, _finite(current_ts, 0.0))
    message_list = list(messages)
    prior = project_group_mood(existing, now=current_ts)
    processed = {
        str(key)
        for key in (prior.get("processed_message_keys") or [])
        if str(key)
    }
    processed_order = list(processed)
    scores: dict[str, float] = {}
    for label in MOOD_LABELS:
        scores[label] = prior.get("mood_scores", {}).get(label, 0.0) if prior else 0.0
    burst: dict[str, float] = {label: 0.0 for label in MOOD_LABELS}
    count = 0
    for index, message in enumerate(message_list):
        key = _message_key(message, index)
        if key in processed:
            continue
        text = _message_text(message)
        if not text:
            continue
        processed.add(key)
        processed_order.append(key)
        count += 1
        lowered = text.lower()
        for pattern, mood, weight in _MOOD_SIGNALS:
            try:
                matches = len(re.findall(pattern, lowered))
            except re.error:
                continue
            if matches:
                burst[mood] = min(100.0, burst[mood] + weight * min(3, matches))
    # Cap each burst contribution so one flood cannot saturate forever.
    for label in MOOD_LABELS:
        if label == "dead_silence":
            scores[label] = round(min(100.0, scores[label]), 2)
            continue
        score = min(100.0, scores[label] + burst[label])
        scores[label] = round(max(0.0, score), 2)
    # Dead silence: an empty / very sparse window raises the silence index.
    message_count = int(prior.get("message_count") or 0) + count
    if count == 0 and not any(_message_text(message) for message in message_list):
        scores["dead_silence"] = round(min(100.0, scores.get("dead_silence", 0.0) + 10.0), 2)
    elif count >= 3:
        scores["dead_silence"] = round(max(0.0, scores.get("dead_silence", 0.0) - 6.0), 2)
    tension = 0.0
    for label in MOOD_LABELS:
        if label == "dead_silence":
            continue
        tension += min(100.0, scores[label]) * _TENSION_WEIGHTS.get(label, 0.0)
    tension = round(min(100.0, tension), 2)
    top_mood = _top_mood(scores)
    return {
        "version": GROUP_MOOD_VERSION,
        "mood_scores": scores,
        "top_mood": top_mood,
        "social_tension": tension,
        "updated_at": current_ts,
        "decayed_at": current_ts,
        "message_count": message_count,
        "processed_message_keys": processed_order[-128:],
    }


def project_group_mood_prompt_facts(
    value: Any,
    *,
    now: Any,
    max_detail: int = 3,
) -> dict[str, Any]:
    """Return structured mood facts for the group prompt adapter."""
    mood = project_group_mood(value, now=now)
    if not mood:
        return {}
    scores = mood.get("mood_scores") or {}
    ranked = [
        label
        for label in sorted(
            MOOD_LABELS,
            key=lambda label: _finite(scores.get(label)),
            reverse=True,
        )
        if _finite(scores.get(label)) >= 6.0
    ]
    top_mood = str(mood.get("top_mood") or "dead_silence")
    detail_moods = [
        {
            "key": label,
            "label": MOOD_LABELS_ZH.get(label, label),
            "score": round(_finite(scores.get(label)), 2),
        }
        for label in ranked[: max(0, int(max_detail))]
        if label != top_mood
    ]
    tension = _finite(mood.get("social_tension"))
    tension_level = "high" if tension >= 55 else "low" if tension <= 12 else "normal"
    return {
        "top_mood": top_mood,
        "top_mood_label": MOOD_LABELS_ZH.get(top_mood, top_mood),
        "top_mood_score": round(_finite(scores.get(top_mood)), 2),
        "detail_moods": detail_moods,
        "social_tension": round(tension, 2),
        "tension_level": tension_level,
    }


def _top_mood(scores: Mapping[str, Any]) -> str:
    if not scores:
        return "dead_silence"
    ranked = sorted(MOOD_LABELS, key=lambda label: _finite(scores.get(label)), reverse=True)
    top_score = _finite(scores.get(ranked[0]))
    if top_score <= 0:
        return "dead_silence"
    return ranked[0]


__all__ = [
    "GROUP_MOOD_VERSION",
    "MOOD_LABELS",
    "MOOD_LABELS_ZH",
    "project_group_mood",
    "settle_group_mood",
    "project_group_mood_prompt_facts",
]
