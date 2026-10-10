"""Bounded, redacted observability for the REQ-041 runtime.

Only fixed labels are accepted.  Identity, group and message identifiers must
never enter this collector, which makes its snapshot safe for the admin page.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import math
import threading
import time
from typing import Any


_CACHE_NAMES = frozenset({"identity", "relationship", "scoped_projection", "memory", "rules"})
_CACHE_OUTCOMES = frozenset({"hit", "miss", "bypass", "stale_reject", "eviction", "cold_start"})
_STAGES = frozenset({
    "identity_namespace", "permission_profile_relationship", "memory_rules",
    "prompt_projection", "persistence_queue", "scoped_sync", "migration_replay",
})
_NAMESPACE_KINDS = frozenset({"private", "group_member", "group_shared", "unknown"})
_COUNTERS = frozenset({
    "cross_scope_denied", "echo_rejected", "reflow_rejected", "identity_rollback",
    "migration_mismatch", "runtime_error", "domain_read",
    "shadow_read_mismatch",
})


def _percentile(samples: list[float], quantile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return round(float(ordered[index]), 3)


class Req041Observability:
    """Thread-safe in-memory metrics with bounded samples and no raw labels."""

    def __init__(self, *, sample_limit: int = 2048, clock: Any = None) -> None:
        self._limit = min(8192, max(64, int(sample_limit)))
        self._clock = clock if callable(clock) else time.time
        self._lock = threading.RLock()
        self._started_at = float(self._clock())
        self._cache_counts: dict[tuple[str, str, str], int] = Counter()
        self._cache_latency: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._limit)
        )
        self._cache_size: dict[str, int] = {}
        self._stage_latency: dict[tuple[str, bool], deque[float]] = defaultdict(
            lambda: deque(maxlen=self._limit)
        )
        self._counters: Counter[str] = Counter()
        self._migration: dict[str, Any] = {
            "state": "unknown", "phase": "", "backlog": 0, "pending": 0,
            "mismatches": 0, "lag_ms": 0.0,
        }

    @staticmethod
    def _kind(value: Any) -> str:
        kind = str(value or "unknown")
        return kind if kind in _NAMESPACE_KINDS else "unknown"

    def cache_event(
        self, cache_name: str, outcome: str, *, namespace_kind: str = "unknown",
        latency_ms: float = 0.0, size: int | None = None,
    ) -> None:
        if cache_name not in _CACHE_NAMES or outcome not in _CACHE_OUTCOMES:
            return
        latency = max(0.0, min(float(latency_ms or 0.0), 600_000.0))
        with self._lock:
            self._cache_counts[(cache_name, outcome, self._kind(namespace_kind))] += 1
            if outcome in {"hit", "miss", "bypass", "stale_reject"}:
                self._cache_latency[cache_name].append(latency)
            if size is not None:
                self._cache_size[cache_name] = max(0, min(int(size), 10_000_000))

    def observe(self, stage: str, latency_ms: float, *, external: bool = False) -> None:
        if stage not in _STAGES:
            return
        latency = max(0.0, min(float(latency_ms or 0.0), 600_000.0))
        with self._lock:
            self._stage_latency[(stage, bool(external))].append(latency)

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in _COUNTERS:
            return
        with self._lock:
            self._counters[name] += max(0, min(int(amount), 1_000_000))

    def migration(
        self, *, state: str = "unknown", phase: str = "", backlog: int = 0,
        pending: int = 0, mismatches: int = 0, lag_ms: float = 0.0,
    ) -> None:
        safe_state = str(state or "unknown")
        if safe_state not in {"unknown", "active", "replaying", "degraded", "paused", "complete"}:
            safe_state = "unknown"
        safe_phase = str(phase or "")
        if safe_phase not in {"", "S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"}:
            safe_phase = ""
        with self._lock:
            self._migration = {
                "state": safe_state, "phase": safe_phase,
                "backlog": max(0, int(backlog)), "pending": max(0, int(pending)),
                "mismatches": max(0, int(mismatches)),
                "lag_ms": round(max(0.0, float(lag_ms or 0.0)), 3),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cache_counts = dict(self._cache_counts)
            cache_latency = {key: list(value) for key, value in self._cache_latency.items()}
            stage_latency = {key: list(value) for key, value in self._stage_latency.items()}
            sizes = dict(self._cache_size)
            counters = dict(self._counters)
            migration = dict(self._migration)
        caches: dict[str, Any] = {}
        for name in sorted(_CACHE_NAMES):
            outcomes = {
                outcome: sum(cache_counts.get((name, outcome, kind), 0) for kind in _NAMESPACE_KINDS)
                for outcome in sorted(_CACHE_OUTCOMES)
            }
            denominator = outcomes["hit"] + outcomes["miss"]
            samples = cache_latency.get(name, [])
            caches[name] = {
                "outcomes": outcomes,
                "hit_rate": round(outcomes["hit"] / denominator, 4) if denominator else None,
                "size": sizes.get(name, 0),
                "latency_ms": {
                    "p50": _percentile(samples, 0.50), "p95": _percentile(samples, 0.95),
                    "p99": _percentile(samples, 0.99), "samples": len(samples),
                },
            }
        stages: dict[str, Any] = {}
        for stage in sorted(_STAGES):
            local = stage_latency.get((stage, False), [])
            external = stage_latency.get((stage, True), [])
            stages[stage] = {
                "local": {"p50": _percentile(local, 0.50), "p95": _percentile(local, 0.95), "p99": _percentile(local, 0.99), "samples": len(local)},
                "external": {"p50": _percentile(external, 0.50), "p95": _percentile(external, 0.95), "p99": _percentile(external, 0.99), "samples": len(external)},
            }
        return {
            "schema_version": 1,
            "started_at": self._started_at,
            "caches": caches,
            "stages": stages,
            "counters": {name: int(counters.get(name, 0)) for name in sorted(_COUNTERS)},
            "migration": migration,
        }


__all__ = ["Req041Observability"]
