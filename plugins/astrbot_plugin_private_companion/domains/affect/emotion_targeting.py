"""High-precision actor/target attribution for short-term emotion events."""

from __future__ import annotations

import re
from typing import Any


def classify_emotion_target(text: Any) -> dict[str, Any]:
    cleaned = " ".join(str(text or "").split())[:1000]
    base = {
        "actor": "user",
        "target": "none",
        "quoted_target": "none",
        "speech_act": "statement",
        "confidence": 0.5,
        "auto_settle": False,
        "reason_code": "target_none",
    }
    if not cleaned:
        return {**base, "confidence": 1.0}
    lower = cleaned.lower()
    if re.search(r"(```|traceback|exception:|\b(error|warning|debug|info)\b[:：]|\{\s*[\"']?[a-z_]+[\"']?\s*:)", lower):
        return {**base, "speech_act": "diagnostic", "confidence": 0.98, "reason_code": "structured_text"}
    quoted = bool(re.search(r"[“\"‘'][^”\"’']{2,160}[”\"’']|(?:^|\n)\s*>\s*\S+|(?:他|她|他们|别人).{0,8}(说|骂|讲)", cleaned))
    negative = bool(re.search(r"(滚|闭嘴|恶心|废物|垃圾|讨厌|烦死|没用|蠢|傻)", cleaned))
    if quoted and negative:
        return {**base, "target": "other", "quoted_target": "ambiguous", "speech_act": "quote", "confidence": 0.92, "reason_code": "quoted_negative"}
    if re.search(r"(我好|我真|我太|我是不是|我就是|我是).{0,12}(废物|垃圾|没用|傻|笨|恶心|讨厌)", cleaned):
        return {**base, "target": "self", "speech_act": "self_disclosure", "confidence": 0.94, "reason_code": "self_negative"}
    direct_bot = bool(
        re.search(r"(讨厌你|烦你|不想理你|你.{0,4}(滚|闭嘴)|你(?:真|也|就是|是|太|真的|怎么这么).{0,8}(恶心|废物|垃圾|没用|太吵|烦死))", cleaned)
        or re.search(r"((bot|机器人|插件|助手|ai).{0,8}(垃圾|废物|恶心|没用)|(垃圾|废物|恶心|没用).{0,8}(bot|机器人|插件|助手|ai))", lower)
    )
    if direct_bot:
        return {**base, "target": "bot", "speech_act": "direct_address", "confidence": 0.94, "auto_settle": True, "reason_code": "direct_bot_target"}
    if re.search(r"(他|她|它|他们|她们|别人|群友|同事|同学|老师|老板|那个人|这个人).{0,18}(滚|闭嘴|恶心|废物|垃圾|讨厌|烦|没用|蠢|傻)", cleaned):
        return {**base, "target": "other", "speech_act": "third_party_report", "confidence": 0.9, "reason_code": "third_party_target"}
    if re.search(r"(对不起|抱歉|我错了|喜欢你|爱你|谢谢你|辛苦了|抱抱你|摸摸你)", cleaned):
        return {**base, "target": "bot", "speech_act": "direct_address", "confidence": 0.86, "auto_settle": True, "reason_code": "direct_positive_target"}
    if negative:
        return {**base, "target": "ambiguous", "speech_act": "ambiguous_negative", "confidence": 0.35, "reason_code": "negative_target_uncertain"}
    return base


__all__ = ["classify_emotion_target"]
