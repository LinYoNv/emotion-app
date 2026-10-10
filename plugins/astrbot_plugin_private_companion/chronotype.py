# -*- coding: utf-8 -*-
"""Per-user chronotype profile: learn wake/sleep anchors from real activity.

The reason windows and proactive hour curve default to a fixed "standard"
schedule. This mixin learns each user's actual rhythm from two sources:

1. Message-time histogram (implicit, self-decaying, no LLM cost).
2. Explicit tells like "我一般两点睡" (high confidence, slow decay).

Nothing here mutates the C3 agenda contract; it only feeds window translation
for outbound proactive timing.
"""
from __future__ import annotations

import datetime
import random
import re
from typing import Any

from .helpers import _safe_float, _safe_int

# 标准作息锚点：7:30 醒、22:30 睡。画像的平移量以它为原点。
_DEFAULT_WAKE_MINUTE = 7 * 60 + 30
_DEFAULT_SLEEP_MINUTE = 22 * 60 + 30
# 显式告知的有效期与直方图学习门槛。
_EXPLICIT_TELL_TTL_SECONDS = 90 * 24 * 3600
_LEARN_MIN_ACTIVE_DAYS = 5
_HISTOGRAM_DECAY_SECONDS = 7 * 24 * 3600
_MAX_SHIFT_MINUTES_LOWER = -3 * 60
_MAX_SHIFT_MINUTES_UPPER = 6 * 60

_CN_DIGITS = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "廿": 20, "半": 30,
}

_TELL_TIME_PATTERN = re.compile(
    r"(凌晨|早上|早晨|清晨|上午|中午|午后|下午|傍晚|晚上|夜里|半夜|深夜)?"
    r"([0-9]{1,2}|[零一二两三四五六七八九十廿]+)"
    r"[点时:：]"
    r"(半|一刻|三刻|([0-9]{1,2}|[一两三四五]?)十?([0-9]|几)?分?)?"
    r"(左右|多|才|再|过后|之后)?"
    r"(睡|睡觉|入睡|就寝|歇|起床|起来|起|睡醒|醒来|醒)"
)
# 必须带习惯性副词，排除"我今天两点才睡"这类一次性陈述。
_TELL_SELF_PATTERN = re.compile(
    r"(我|俺|人家|本人)(一般|通常|平时|习惯|总是|基本上?|大多|经常)"
)
_WAKE_VERBS = {"起床", "起来", "起", "睡醒", "醒来", "醒"}


def _cn_number_to_int(text: str) -> int | None:
    text = str(text or "").strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 0 <= value <= 24 else None
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    # 十位组合：二十三、十几、廿几。
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        value = tens * 10 + ones
        return value if 0 <= value <= 24 else None
    if len(text) == 2 and text[0] == "廿":
        value = 20 + _CN_DIGITS.get(text[1], 0)
        return value if 0 <= value <= 24 else None
    return None


def _tell_minute_parts(match: re.Match[str]) -> tuple[int, int] | None:
    daypart = match.group(1) or ""
    hour_raw = match.group(2) or ""
    hour = _cn_number_to_int(hour_raw)
    if hour is None:
        return None
    minute_part = match.group(3) or ""
    minute = 0
    if minute_part == "半":
        minute = 30
    elif minute_part == "一刻":
        minute = 15
    elif minute_part == "三刻":
        minute = 45
    elif minute_part.endswith("分"):
        digits = minute_part[:-1]
        parsed = _cn_number_to_int(digits) if digits else 0
        minute = parsed if parsed is not None and 0 <= parsed <= 59 else 0
    verb = match.group(7) or ""
    if daypart in {"中午", "午后"}:
        hour = 12 if hour == 12 else hour + 12 if hour < 12 and hour > 0 else hour
    elif daypart in {"下午", "傍晚"} and 0 < hour < 12:
        hour += 12
    elif daypart in {"晚上", "夜里", "半夜", "深夜"} and 0 < hour < 12:
        hour += 12
    elif not daypart:
        # 无时段前缀：睡/起床动词补默认语义（"2点睡"=凌晨2点，"7点起"=早上7点）。
        if verb in _WAKE_VERBS and hour >= 20:
            hour -= 12
        if verb not in _WAKE_VERBS and 7 <= hour <= 12:
            # "11点睡"在中文口语里几乎总是夜里 23 点。
            hour += 12
    hour = hour % 24
    return hour * 60 + minute, (1 if verb in _WAKE_VERBS else 0)


class ChronotypeMixin:
    """Learn and expose per-user wake/sleep anchors for proactive timing."""

    def _note_user_chronotype_from_inbound(
        self,
        user: dict[str, Any] | None,
        text: str,
        ts: float,
    ) -> None:
        """中央入站钩子：直方图与显式告知共用一个入口，失败不影响回复链。"""
        try:
            self._note_user_chronotype_activity(user, ts)
            self._note_user_chronotype_tell(user, text, now=ts)
        except Exception:
            return

    def _user_chronotype_store(self, user: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(user, dict):
            return {}
        store = user.get("chronotype_profile")
        if not isinstance(store, dict):
            store = {}
            user["chronotype_profile"] = store
        if not isinstance(store.get("hour_histogram"), list) or len(store.get("hour_histogram", [])) != 24:
            store["hour_histogram"] = [0] * 24
        if not isinstance(store.get("hist_active_days"), list):
            store["hist_active_days"] = []
        if "hist_decayed_at" not in store:
            store["hist_decayed_at"] = 0.0
        if "explicit_wake_minute" not in store:
            store["explicit_wake_minute"] = -1
        if "explicit_sleep_minute" not in store:
            store["explicit_sleep_minute"] = -1
        if "explicit_at" not in store:
            store["explicit_at"] = 0.0
        if "learned_wake_minute" not in store:
            store["learned_wake_minute"] = -1
        if "learned_sleep_minute" not in store:
            store["learned_sleep_minute"] = -1
        return store

    def _note_user_chronotype_activity(
        self,
        user: dict[str, Any] | None,
        ts: float,
        *,
        now_dt: Any = None,
    ) -> None:
        """Record one inbound activity into the histogram and refresh anchors."""
        store = self._user_chronotype_store(user)
        if not store or ts <= 0:
            return
        dt_getter = getattr(self, "_environment_fromtimestamp", None)
        try:
            local = now_dt if now_dt is not None else (
                dt_getter(ts) if callable(dt_getter) else None
            )
            hour = int(local.hour)
            day_key = local.strftime("%Y-%m-%d")
        except Exception:
            return
        if not 0 <= hour <= 23:
            return
        histogram = store["hour_histogram"]
        histogram[hour] = _safe_int(histogram[hour], 0, 0) + 1
        days = store["hist_active_days"]
        if day_key not in days:
            days.append(day_key)
            days[:] = days[-14:]
        now_seconds = float(ts)
        if now_seconds - _safe_float(store.get("hist_decayed_at"), 0) > _HISTOGRAM_DECAY_SECONDS:
            histogram[:] = [int(value / 2) for value in histogram]
            store["hist_decayed_at"] = now_seconds
        if len(days) >= _LEARN_MIN_ACTIVE_DAYS:
            self._refresh_learned_chronotype(store)

    def _refresh_learned_chronotype(self, store: dict[str, Any]) -> None:
        histogram = store.get("hour_histogram")
        if not isinstance(histogram, list) or len(histogram) != 24:
            return
        total = sum(_safe_int(value, 0, 0) for value in histogram)
        if total < 40:
            return
        mean = total / 24.0
        low_threshold = max(0.5, mean * 0.18)
        low_flags = [1 if _safe_int(value, 0, 0) <= low_threshold else 0 for value in histogram]
        if all(low_flags) or not any(low_flags):
            return
        # 最长环形低活跃段 = 睡眠时段；长度 4–12 小时之外视为噪声。
        best_len = 0
        best_end = -1
        run = 0
        for offset in range(48):
            flag = low_flags[offset % 24]
            if flag:
                run += 1
                if run > best_len:
                    best_len = run
                    best_end = offset % 24
            else:
                run = 0
        if not 4 <= best_len <= 12:
            return
        wake_hour = (best_end + 1) % 24
        sleep_hour = (best_end - best_len + 1) % 24
        wake_minute = wake_hour * 60
        sleep_minute = sleep_hour * 60
        if wake_minute == sleep_minute:
            return
        store["learned_wake_minute"] = wake_minute
        store["learned_sleep_minute"] = sleep_minute

    def _extract_user_chronotype_tell(self, text: str) -> dict[str, Any] | None:
        raw = str(text or "")
        if not raw:
            return None
        for match in _TELL_TIME_PATTERN.finditer(raw):
            window = raw[max(0, match.start() - 8):match.start()]
            if not _TELL_SELF_PATTERN.search(window):
                continue
            parts = _tell_minute_parts(match)
            if parts is None:
                continue
            minute, is_wake = parts
            kind = "wake" if is_wake else "sleep"
            return {"kind": kind, "minute": minute, "raw": match.group(0)[:40]}
        return None

    def _note_user_chronotype_tell(
        self,
        user: dict[str, Any] | None,
        text: str,
        *,
        now: float | None = None,
    ) -> bool:
        store = self._user_chronotype_store(user)
        if not store:
            return False
        tell = self._extract_user_chronotype_tell(text)
        if not tell:
            return False
        now_getter = getattr(self, "_now_ts", None)
        ts = now if now is not None else (now_getter() if callable(now_getter) else 0.0)
        if tell["kind"] == "wake":
            store["explicit_wake_minute"] = int(tell["minute"])
        else:
            store["explicit_sleep_minute"] = int(tell["minute"])
        store["explicit_at"] = float(ts)
        return True

    def _user_chronotype(self, user: dict[str, Any] | None, *, now: float | None = None) -> dict[str, Any]:
        store = self._user_chronotype_store(user)
        result = {
            "wake_minute": _DEFAULT_WAKE_MINUTE,
            "sleep_minute": _DEFAULT_SLEEP_MINUTE,
            "source": "default",
            "confidence": 0.0,
        }
        if not store:
            return result
        now_getter = getattr(self, "_now_ts", None)
        now_seconds = now if now is not None else (now_getter() if callable(now_getter) else 0.0)
        explicit_fresh = (
            _safe_float(store.get("explicit_at"), 0) > 0
            and now_seconds - _safe_float(store.get("explicit_at"), 0) <= _EXPLICIT_TELL_TTL_SECONDS
        )
        explicit_wake = _safe_int(store.get("explicit_wake_minute"), -1, -1)
        explicit_sleep = _safe_int(store.get("explicit_sleep_minute"), -1, -1)
        sources: list[tuple[str, float]] = []
        if explicit_fresh:
            if 0 <= explicit_wake < 24 * 60:
                result["wake_minute"] = explicit_wake
                sources.append(("explicit", 0.9))
            if 0 <= explicit_sleep < 24 * 60:
                result["sleep_minute"] = explicit_sleep
                sources.append(("explicit", 0.9))
        learned_wake = _safe_int(store.get("learned_wake_minute"), -1, -1)
        learned_sleep = _safe_int(store.get("learned_sleep_minute"), -1, -1)
        if 0 <= learned_wake < 24 * 60 and 0 <= learned_sleep < 24 * 60:
            wake_set_explicitly = explicit_fresh and 0 <= explicit_wake < 24 * 60
            sleep_set_explicitly = explicit_fresh and 0 <= explicit_sleep < 24 * 60
            if not wake_set_explicitly:
                result["wake_minute"] = learned_wake
            if not sleep_set_explicitly:
                result["sleep_minute"] = learned_sleep
            sources.append(("learned", 0.6))
        if sources:
            result["source"] = sources[0][0]
            result["confidence"] = max(confidence for _, confidence in sources)
        shift = result["wake_minute"] - _DEFAULT_WAKE_MINUTE
        result["shift_minutes"] = int(max(_MAX_SHIFT_MINUTES_LOWER, min(_MAX_SHIFT_MINUTES_UPPER, shift)))
        return result

    def _chronotype_reason_shift(self, user: dict[str, Any] | None, *, now: float | None = None) -> int:
        profile = self._user_chronotype(user, now=now)
        if profile.get("source") == "default":
            return 0
        return _safe_int(profile.get("shift_minutes"), 0, _MAX_SHIFT_MINUTES_LOWER, _MAX_SHIFT_MINUTES_UPPER)

    @staticmethod
    def _shift_reason_windows(windows: list[tuple[int, int]], shift: int) -> list[tuple[int, int]]:
        """Translate windows by shift minutes, splitting spans that cross midnight."""
        if not shift:
            return list(windows)
        shifted: list[tuple[int, int]] = []
        for start, end in windows:
            span = int(end) - int(start)
            if span <= 0 or span >= 24 * 60:
                shifted.append((int(start), int(end)))
                continue
            new_start = (int(start) + shift) % (24 * 60)
            new_end = new_start + span
            if new_end <= 24 * 60:
                shifted.append((new_start, new_end))
            else:
                shifted.append((new_start, 24 * 60))
                shifted.append((0, new_end - 24 * 60))
        shifted.sort(key=lambda item: item[0])
        return shifted

    def _chronotype_hour_weights(
        self,
        user: dict[str, Any] | None,
        base: list[float],
    ) -> list[float]:
        """Blend the global hour curve with the user's own activity histogram."""
        if not isinstance(base, list) or len(base) != 24:
            return list(base) if isinstance(base, list) else []
        store = self._user_chronotype_store(user)
        if not store:
            return list(base)
        histogram = store.get("hour_histogram")
        if not isinstance(histogram, list) or len(histogram) != 24:
            return list(base)
        total = sum(_safe_int(value, 0, 0) for value in histogram)
        if total < 40:
            return list(base)
        mean = total / 24.0
        blended: list[float] = []
        for index, base_weight in enumerate(base):
            share = _safe_int(histogram[index], 0, 0) / max(1.0, mean)
            factor = max(0.4, min(1.6, 0.4 + 0.6 * share))
            blended.append(max(0.05, min(2.0, float(base_weight) * factor)))
        return blended

    def _is_currently_active(
        self, user: dict[str, Any] | None = None, *, now: float | None = None
    ) -> bool:
        """Check if the current time falls within the user's active window."""
        profile = self._user_chronotype(user, now=now)
        wake_minute = profile.get("wake_minute", _DEFAULT_WAKE_MINUTE)
        sleep_minute = profile.get("sleep_minute", _DEFAULT_SLEEP_MINUTE)
        now_getter = getattr(self, "_now_ts", None)
        now_ts = now if now is not None else (now_getter() if callable(now_getter) else 0.0)
        if now_ts <= 0:
            now_ts = datetime.datetime.now().timestamp()
        dt_getter = getattr(self, "_environment_fromtimestamp", None)
        try:
            local = dt_getter(now_ts) if callable(dt_getter) else datetime.datetime.fromtimestamp(now_ts)
            current_minute = local.hour * 60 + local.minute
        except Exception:
            return True
        if wake_minute <= sleep_minute:
            return wake_minute <= current_minute <= sleep_minute
        # Overnight sleep: active from wake_minute through midnight, then midnight to sleep_minute
        return current_minute >= wake_minute or current_minute <= sleep_minute

    def _swing_probability(
        self,
        base_probability: float,
        *,
        swing_factor: float = 0.15,
        active_boost: float = 0.25,
        user: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> float:
        """Apply random swing and active-hour boost to a probability.

        - ``swing_factor`` controls the random oscillation range (default ±15%).
        - ``active_boost`` adds a multiplicative boost during active hours (default +25%).
        - The result is clamped to [0.0, 1.0].
        """
        prob = float(base_probability)
        # Random swing
        swing = 1.0 + random.uniform(-swing_factor, swing_factor)
        prob *= swing
        # Active-hour boost
        if self._is_currently_active(user, now=now):
            prob *= 1.0 + active_boost
        return max(0.0, min(1.0, prob))
