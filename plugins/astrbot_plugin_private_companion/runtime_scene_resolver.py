# -*- coding: utf-8 -*-
"""Bot-only current-state resolver with TTL and idempotent commits."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _now(value: datetime | None = None, timezone_name: str = "Asia/Shanghai") -> datetime:
    current = value or datetime.now().astimezone()
    try:
        timezone = ZoneInfo(_text(timezone_name, 64) or "Asia/Shanghai")
    except Exception:
        timezone = datetime.now().astimezone().tzinfo
    if current.tzinfo:
        return current.astimezone(timezone)
    return current.replace(tzinfo=timezone)


def _safe_seconds(value: Any, default: int = 1800) -> int:
    try:
        return max(1, min(4 * 3600, int(float(value))))
    except (TypeError, ValueError):
        return default


SELF_STATE_ALLOWED = frozenset({"在休息", "陪你聊天", "准备出门", "切到学习状态"})
SELF_STATE_ALIASES = {
    "在聊天": "陪你聊天",
    "还在陪你聊天": "陪你聊天",
    "休息": "在休息",
    "出门": "准备出门",
    "学习": "切到学习状态",
}
SELF_STATE_FORBIDDEN = (
    "付款",
    "支付",
    "发布",
    "定位",
    "签到",
    "打卡",
    "到场",
    "通话",
    "上课",
    "上班",
    "刷穿搭",
    "拉床帘",
    "arrived",
    "check-in",
    "checked in",
    "paid",
    "publish",
    "payment",
    "location",
    "call",
)


@dataclass(frozen=True)
class SelfStateCommit:
    actor_type: str
    subject_actor_id: str
    state: str
    committed_at: str
    valid_until: str
    runtime_origin_refs: tuple[str, ...]
    source_refs: tuple[str, ...] = ()
    evidence_kind: str = "self_state_commit"
    evidence_level: str = "L1"
    fact_eligibility: str = "current_internal"
    materialization_state: str = "active"
    content_granularity: str = "intent"
    idempotency_key: str = ""
    state_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_type": self.actor_type,
            "subject_actor_id": self.subject_actor_id,
            "state": self.state,
            "title": self.state,
            "committed_at": self.committed_at,
            "valid_until": self.valid_until,
            "runtime_origin_refs": list(self.runtime_origin_refs),
            "source_refs": list(self.source_refs),
            "evidence_kind": self.evidence_kind,
            "evidence_level": self.evidence_level,
            "fact_eligibility": self.fact_eligibility,
            "materialization_state": self.materialization_state,
            "content_granularity": self.content_granularity,
            "idempotency_key": self.idempotency_key,
            "state_version": self.state_version,
            # Runtime state is not a C3 lifecycle fact.  Keep it independent
            # from active/completed/history statuses while exposing its
            # current_internal eligibility.
            "status": "planned",
            "source_kind": "observed",
        }


class RuntimeSceneResolver:
    """规则优先地把当前时段落定为宽泛 Bot 内部状态。

    候选永远只是输入，只有本类在当前窗口通过 CAS 提交后才成为
    ``current_internal``。提交不会修改原始计划，也不会写入历史或长期记忆。
    """

    def __init__(
        self,
        *,
        bot_id: str = "bot_self",
        clock: Callable[[], datetime] | None = None,
        default_ttl_seconds: int = 1800,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self.bot_id = _text(bot_id, 120) or "bot_self"
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.default_ttl_seconds = _safe_seconds(default_ttl_seconds)
        self.timezone_name = _text(timezone_name, 64) or "Asia/Shanghai"
        self._commits: dict[str, dict[str, Any]] = {}
        self._versions: dict[str, int] = {}
        self._clock_skew_tolerance = timedelta(seconds=1)

    def _current(self, now: datetime | None = None) -> datetime:
        try:
            value = self._clock() if now is None else now
        except Exception:
            value = now or datetime.now().astimezone()
        return _now(value, self.timezone_name)

    def _clock_current(self) -> datetime:
        return self._current(None)

    @staticmethod
    def _normalize_state(value: Any) -> str:
        text = _text(value, 120)
        if text in SELF_STATE_ALLOWED:
            return text
        return SELF_STATE_ALIASES.get(text, "")

    def _is_backfill(self, requested: datetime | None) -> bool:
        if requested is None:
            return False
        return self._current(requested) < self._clock_current() - self._clock_skew_tolerance

    @staticmethod
    def _window_id(now: datetime, hard_constraints: Any = None) -> str:
        if isinstance(hard_constraints, dict) and hard_constraints.get("window_id"):
            return _text(hard_constraints.get("window_id"), 160)
        return now.strftime("%Y-%m-%d:%H")

    @staticmethod
    def _commit_ts(value: dict[str, Any] | None) -> float:
        if not isinstance(value, dict):
            return 0.0
        try:
            stamp = datetime.fromisoformat(str(value.get("committed_at") or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0.0
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return stamp.timestamp()

    def _prune_windows(self, now: datetime | None = None) -> None:
        """Drop windows that can no longer be valid so hourly scene commits
        cannot accumulate without bound on a long-running host."""
        current = self._current(now)
        if not self._commits:
            return
        expired = [key for key, item in self._commits.items() if not self._is_valid(item, current)]
        for key in expired:
            self._commits.pop(key, None)
            self._versions.pop(key, None)
        # Defense-in-depth hard cap for custom (non-hourly) window ids.
        overflow = len(self._commits) - 1024
        if overflow > 0:
            oldest = sorted(self._commits.items(), key=lambda kv: self._commit_ts(kv[1]))[:overflow]
            for key, _ in oldest:
                self._commits.pop(key, None)
                self._versions.pop(key, None)

    @staticmethod
    def _conversation_active(state: Any) -> bool:
        if isinstance(state, dict):
            if state.get("interacting") or state.get("active") or state.get("conversation_active"):
                return True
            text = _text(state.get("last_user_message") or state.get("topic"), 300)
            return bool(text)
        return bool(state)

    def _candidate_state(self, candidates: Any) -> str:
        if not isinstance(candidates, (list, tuple)):
            return ""
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            actor_type = _text(candidate.get("actor_type"), 32).lower()
            subject = _text(candidate.get("subject_actor_id"), 120)
            if actor_type != "bot" or subject != self.bot_id:
                continue
            state = _text(candidate.get("state") or candidate.get("intent") or candidate.get("title") or candidate.get("activity"), 120)
            lowered = state.lower()
            # Scene details are deliberately collapsed to a safe intent.
            if any(token in lowered for token in ("chat", "conversation", "聊天", "对话", "陪聊")):
                return "陪你聊天"
            if any(token in lowered for token in ("rest", "sleep", "休息", "睡", "放松")):
                return "在休息"
            if any(token in lowered for token in ("study", "学习", "专注")):
                return "切到学习状态"
            if any(token in lowered for token in ("out", "leave", "出门", "准备出门")):
                return "准备出门"
        return ""

    def _make_commit(
        self,
        *,
        state: str,
        now: datetime,
        window_id: str,
        state_version: int,
        ttl_seconds: int,
        origin_refs: list[str],
    ) -> dict[str, Any]:
        valid_until = now + timedelta(seconds=_safe_seconds(ttl_seconds, self.default_ttl_seconds))
        key = f"runtime_commit:{self.bot_id}:{window_id}:{state_version}"
        commit = SelfStateCommit(
            actor_type="bot",
            subject_actor_id=self.bot_id,
            state=state,
            committed_at=now.isoformat(timespec="seconds"),
            valid_until=valid_until.isoformat(timespec="seconds"),
            runtime_origin_refs=tuple(dict.fromkeys([_text(ref, 160) for ref in origin_refs if _text(ref, 160)])) or (f"resolver:{window_id}",),
            idempotency_key=key,
            state_version=state_version,
        )
        result = commit.to_dict()
        result["window_id"] = window_id
        return result

    def commit(
        self,
        state: str,
        *,
        now: datetime | None = None,
        window_id: str = "",
        expected_version: int | None = None,
        ttl_seconds: int | None = None,
        origin_refs: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        if self._is_backfill(now):
            return None
        current = self._current(now)
        self._prune_windows(current)
        raw_state = _text(state, 120)
        if any(token in raw_state.lower() for token in SELF_STATE_FORBIDDEN):
            return None
        clean_state = self._normalize_state(raw_state)
        if not clean_state:
            return None
        window = _text(window_id, 160) or self._window_id(current)
        prior = self._commits.get(window)
        prior_version = int(prior.get("state_version") or 0) if isinstance(prior, dict) else self._versions.get(window, 0)
        if expected_version is not None:
            try:
                if int(expected_version) != prior_version:
                    return None
            except (TypeError, ValueError):
                return None
        if prior and self._is_valid(prior, current) and _text(prior.get("state"), 120) == clean_state:
            return deepcopy(prior)
        version = prior_version + 1
        result = self._make_commit(
            state=clean_state,
            now=current,
            window_id=window,
            state_version=version,
            ttl_seconds=ttl_seconds or self.default_ttl_seconds,
            origin_refs=list(origin_refs or ()),
        )
        self._versions[window] = version
        self._commits[window] = deepcopy(result)
        return deepcopy(result)

    def resolve_now(
        self,
        agenda_candidates: Any,
        conversation_state: Any = None,
        hard_constraints: Any = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if self._is_backfill(now):
            return None
        current = self._current(now)
        window = self._window_id(current, hard_constraints)
        conversation_active = self._conversation_active(conversation_state)
        prior = self._commits.get(window)
        if prior and self._is_valid(prior, current):
            # A new user turn interrupts a previously inferred state. Repeated
            # calls with the same state remain idempotent within the window.
            prior_state = _text(prior.get("state"), 120).lower()
            chat_state = "陪你聊天"
            if not conversation_active or prior_state in {chat_state.lower(), "chat", "conversation"}:
                return deepcopy(prior)
            return self.commit(
                chat_state,
                now=current,
                window_id=window,
                expected_version=int(prior.get("state_version") or 0),
                origin_refs=[f"window:{window}", "conversation:true", "interruption:user_turn"],
            )
        if conversation_active:
            state = "陪你聊天"
        else:
            state = self._candidate_state(agenda_candidates)
            if not state:
                # No current evidence means unknown, not resting.  A default
                # rest commit otherwise renews every hour and becomes a fake
                # persistent scene in the panel and prompts.
                return None
        origin_refs = [f"window:{window}", f"conversation:{bool(conversation_active)}"]
        if isinstance(hard_constraints, dict):
            ref = _text(hard_constraints.get("event_id") or hard_constraints.get("id"), 160)
            if ref:
                origin_refs.append(f"constraint:{ref}")
        return self.commit(state, now=current, window_id=window, origin_refs=origin_refs)

    @staticmethod
    def _is_valid(commit: dict[str, Any], now: datetime) -> bool:
        if not isinstance(commit, dict):
            return False
        try:
            if str(commit.get("actor_type") or "").strip().lower() != "bot":
                return False
            if not str(commit.get("subject_actor_id") or "").strip():
                return False
            if str(commit.get("state") or "").strip() not in SELF_STATE_ALLOWED:
                return False
            if commit.get("source_refs") not in (None, [], (), ""):
                return False
            if not commit.get("runtime_origin_refs"):
                return False
            committed = datetime.fromisoformat(str(commit.get("committed_at")).replace("Z", "+00:00"))
            until = datetime.fromisoformat(str(commit.get("valid_until")).replace("Z", "+00:00"))
            current = now if now.tzinfo else now.astimezone()
            committed = committed if committed.tzinfo else committed.replace(tzinfo=current.tzinfo)
            until = until if until.tzinfo else until.replace(tzinfo=current.tzinfo)
            return committed <= current < until and commit.get("materialization_state") == "active" and commit.get("fact_eligibility") == "current_internal"
        except (TypeError, ValueError):
            return False

    def get_current(self, now: datetime | None = None, *, window_id: str = "") -> dict[str, Any] | None:
        if self._is_backfill(now):
            return None
        current = self._current(now)
        window = _text(window_id, 160) or self._window_id(current)
        item = self._commits.get(window)
        return deepcopy(item) if item and self._is_valid(item, current) else None

    def invalidate(
        self,
        *,
        now: datetime | None = None,
        window_id: str = "",
        reason: str = "",
        expected_version: int | None = None,
    ) -> bool:
        if self._is_backfill(now):
            return False
        current = self._current(now)
        window = _text(window_id, 160) or self._window_id(current)
        prior = self._commits.get(window)
        if not prior:
            return False
        if expected_version is not None:
            try:
                if int(expected_version) != int(prior.get("state_version") or 0):
                    return False
            except (TypeError, ValueError):
                return False
        prior["materialization_state"] = "expired"
        prior["valid_until"] = current.isoformat(timespec="seconds")
        trace = prior.setdefault("decision_trace", [])
        if isinstance(trace, list):
            trace.append({"code": "invalidated", "reason": _text(reason, 160) or "runtime interruption"})
        return True

    def clear(self) -> None:
        self._commits.clear()
        self._versions.clear()


__all__ = ["RuntimeSceneResolver", "SelfStateCommit"]
