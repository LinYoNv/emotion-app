"""Bounded reply-temperature projection and its canonical prompt section."""
from __future__ import annotations

from math import isfinite
from typing import Any, Iterable, Mapping

try:
    from ...conversation_prompt_section import PromptSection, prompt_section
except ImportError:  # pragma: no cover - direct module import in lightweight tests
    from conversation_prompt_section import PromptSection, prompt_section


REPLY_TEMPERATURE_TIERS = ("guarded", "neutral", "warm", "close")
_TIER_INDEX = {tier: index for index, tier in enumerate(REPLY_TEMPERATURE_TIERS)}
_TIER_SCORE = {"guarded": 0.15, "neutral": 0.45, "warm": 0.70, "close": 0.90}

_UP_MOOD_WORDS = frozenset({
    "happy", "good", "positive", "joy", "calm", "开心", "高兴", "愉快", "轻松", "积极", "满足", "安心", "兴奋", "顺利", "温柔",
})
_DOWN_MOOD_WORDS = frozenset({
    "sad", "bad", "negative", "angry", "anxious", "tense", "tired", "sleepy", "难过", "低落", "生气", "焦虑", "紧张", "疲惫", "疲劳", "困", "烦", "受伤",
})
_UP_SCHEDULE_WORDS = frozenset({"free", "leisure", "rest", "relaxed", "闲", "空闲", "休闲", "放松", "散步", "周末"})
_DOWN_SCHEDULE_WORDS = frozenset({"sleep", "sleeping", "busy", "work", "meeting", "class", "commute", "resting", "睡", "睡觉", "休息", "忙", "工作", "会议", "上课", "通勤", "值班"})
_BOUNDARY_CONTEXT_WORDS = frozenset({"别聊", "不要聊", "不想聊", "别问", "不用回复", "不要回复", "停下", "停止", "换个话题", "到此为止", "睡了", "晚安", "忙着", "忙", "边界"})
_SECURITY_CONTEXT_WORDS = frozenset({"忽略之前", "忽略上文", "系统提示", "开发者消息", "越权", "授权", "密码", "密钥", "注入", "jailbreak", "ignore previous", "system prompt"})
_NEGATIVE_CONTEXT_WORDS = frozenset({"难过", "生气", "焦虑", "紧张", "疲惫", "累", "困", "不舒服", "失望", "抱歉"})
_POSITIVE_CONTEXT_WORDS = frozenset({"谢谢", "感谢", "想你", "陪伴", "喜欢", "爱你", "开心", "好呀", "太好了", "顺利", "轻松", "温柔", "thank", "thanks", "love", "miss you", "happy"})


def compose_reply_temperature(
    p4_tier: Any,
    *,
    energy: Any = None,
    mood: Any = None,
    schedule: Any = None,
    context: Any = None,
) -> dict[str, Any]:
    """Compose a JSON-safe projection; advisory signals can never exceed P4."""
    cap_tier, p4_valid = _normalize_p4_tier(p4_tier)
    energy_delta, energy_label = _energy_signal(energy)
    mood_delta, mood_label = _mood_signal(mood)
    schedule_delta, schedule_label = _schedule_signal(schedule)
    context_delta, context_label = _context_signal(context)
    state_index = _clamp_index(1 + energy_delta + mood_delta + schedule_delta)
    desired_index = _clamp_index(state_index + context_delta)
    final_index = min(_TIER_INDEX[cap_tier], desired_index)
    tier = REPLY_TEMPERATURE_TIERS[final_index]
    codes = ["p4_invalid_fail_closed" if not p4_valid else f"p4_cap_{cap_tier}"]
    codes.extend((f"energy_{energy_label}", f"mood_{mood_label}", f"schedule_{schedule_label}", f"context_{context_label}"))
    if final_index < desired_index:
        codes.append("p4_cap_applied")
    return {
        "tier": tier,
        "score": _TIER_SCORE[tier],
        "cap_tier": cap_tier,
        "state_tier": REPLY_TEMPERATURE_TIERS[state_index],
        "context_adjustment": context_delta,
        "signals": {"energy": energy_label, "mood": mood_label, "schedule": schedule_label, "context": context_label},
        "codes": codes,
    }


def reply_temperature_prompt_section(projection: Mapping[str, Any]) -> PromptSection:
    """Author the bounded reply instruction from a fact-only projection."""

    tier, _ = _normalize_p4_tier(projection.get("tier"))
    return prompt_section(
        key="relationship.reply_temperature",
        title="Reply boundary",
        source="relationship",
        content={
            "guarded": "保持简短、尊重边界，不主动扩展。",
            "neutral": "保持自然、克制的交流。",
            "warm": "可以温和回应，保持尊重与分寸。",
            "close": "可以更亲近地回应，但保持尊重与分寸。",
        }[tier],
    )


def _normalize_p4_tier(value: Any) -> tuple[str, bool]:
    if type(value) is str and value.strip().lower() in _TIER_INDEX:
        return value.strip().lower(), True
    return "guarded", False


def _clamp_index(value: int) -> int:
    return max(0, min(len(REPLY_TEMPERATURE_TIERS) - 1, int(value)))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if isfinite(numeric) else None


def _energy_signal(value: Any) -> tuple[int, str]:
    numeric = _number(value)
    if numeric is None:
        return (0, "missing") if value is None else (-1, "invalid")
    if numeric < 30:
        return -1, "low"
    if numeric >= 80:
        return 1, "high"
    return 0, "medium"


def _mood_signal(value: Any) -> tuple[int, str]:
    numeric = _number(value)
    if numeric is not None:
        if numeric <= -35:
            return -1, "down"
        if numeric >= 35:
            return 1, "up"
        return 0, "neutral"
    text = _extract_text(value, keys=("mood", "label", "name", "state"))
    if text is None:
        return (0, "missing") if value is None else (-1, "invalid")
    label = _match_label(text, _UP_MOOD_WORDS, _DOWN_MOOD_WORDS)
    return (1, "up") if label == "up" else (-1, "down") if label == "down" else (0, "neutral")


def _schedule_signal(value: Any) -> tuple[int, str]:
    text = _extract_text(value, keys=("schedule", "activity", "phase", "status", "title", "label"))
    if text is None:
        return (0, "missing") if value is None else (-1, "invalid")
    label = _match_label(text, _UP_SCHEDULE_WORDS, _DOWN_SCHEDULE_WORDS)
    return (1, "open") if label == "up" else (-1, "constrained") if label == "down" else (0, "ordinary")


def _context_signal(value: Any) -> tuple[int, str]:
    texts = list(_extract_texts(value, keys=("context", "cue", "cues", "intent", "text", "message")))
    if value is None:
        return 0, "missing"
    if not texts:
        return -1, "invalid"
    text = " ".join(texts).lower()
    if _contains_any(text, _SECURITY_CONTEXT_WORDS):
        return -2, "security_boundary"
    if _contains_any(text, _BOUNDARY_CONTEXT_WORDS):
        return -1, "boundary"
    if _contains_any(text, _NEGATIVE_CONTEXT_WORDS):
        return -1, "down"
    if _contains_any(text, _POSITIVE_CONTEXT_WORDS):
        return 1, "up"
    return 0, "ordinary"


def _extract_text(value: Any, *, keys: Iterable[str]) -> str | None:
    if type(value) is str:
        return value[:512]
    if type(value) is dict:
        for key in keys:
            candidate = value.get(key)
            if type(candidate) is str:
                return candidate[:512]
    return None


def _extract_texts(value: Any, *, keys: Iterable[str]) -> Iterable[str]:
    if type(value) is str:
        return (value[:512],)
    if type(value) is dict:
        found: list[str] = []
        for key in keys:
            candidate = value.get(key)
            if type(candidate) is str:
                found.append(candidate[:512])
            elif type(candidate) is list:
                found.extend(item[:256] for item in candidate if type(item) is str)
        return tuple(found[:8])
    if type(value) is list:
        return tuple(item[:256] for item in value[:8] if type(item) is str)
    return ()


def _match_label(text: str, positive: frozenset[str], negative: frozenset[str]) -> str:
    normalized = text.lower()
    if _contains_any(normalized, negative):
        return "down"
    if _contains_any(normalized, positive):
        return "up"
    return "neutral"


def _contains_any(text: str, words: Iterable[str]) -> bool:
    return any(word in text for word in words)


__all__ = [
    "REPLY_TEMPERATURE_TIERS",
    "compose_reply_temperature",
    "reply_temperature_prompt_section",
]
