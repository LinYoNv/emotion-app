from __future__ import annotations

from typing import Any, Mapping

try:
    from .conversation_prompt_section import PromptSection, prompt_section
except ImportError:  # pragma: no cover - direct module import in lightweight tests
    from conversation_prompt_section import PromptSection, prompt_section


_CYCLE_PHASES = {
    "menstrual",
    "follicular",
    "pre_ovulation",
    "ovulation",
    "luteal",
    "pms",
    "pre",
    "recovery",
    "period",
}
_RELATED_MARKERS = (
    "月经",
    "经期",
    "生理期",
    "例假",
    "姨妈",
    "痛经",
    "卫生巾",
    "小腹",
    "肚子痛",
)
_HIGHLY_PRIVATE_MARKERS = (
    "做爱",
    "性交",
    "性爱",
    "性行为",
    "自慰",
    "裸体",
    "脱衣",
    "私处",
    "发情",
    "性欲",
)


def cycle_phase_from_label(value: Any) -> str:
    text = str(value or "")
    upper = text.upper()
    if not text or text in {"无明显周期影响", "不处于生理期"}:
        return ""
    if "PMS" in upper or "经前综合征" in text:
        return "pms"
    if "排卵前期" in text:
        return "pre_ovulation"
    if "月经期" in text:
        return "menstrual"
    if "卵泡期" in text:
        return "follicular"
    if "排卵期" in text:
        return "ovulation"
    if "黄体期" in text:
        return "luteal"
    if "生理期后" in text or "恢复" in text:
        return "recovery"
    if "生理期" in text:
        return "period"
    if "前" in text:
        return "pre"
    return ""


def build_group_cycle_boundary(
    *,
    enabled: bool,
    group_allowed: bool,
    cycle_label: Any,
    inbound_text: Any,
) -> dict[str, Any]:
    """Build a bounded group-only projection without echoing conversation data."""
    phase = cycle_phase_from_label(cycle_label)
    if not enabled or not group_allowed or phase not in _CYCLE_PHASES:
        return {"active": False, "phase": "", "topic_related": False, "private_boundary": False}

    compact = str(inbound_text or "").lower()
    topic_related = any(marker in compact for marker in _RELATED_MARKERS)
    private_boundary = phase == "menstrual" and any(marker in compact for marker in _HIGHLY_PRIVATE_MARKERS)
    return {
        "active": True,
        "phase": phase,
        "topic_related": topic_related,
        "private_boundary": private_boundary,
    }


def group_cycle_boundary_prompt_section(
    projection: Mapping[str, Any],
) -> PromptSection | None:
    """Author the group-cycle privacy boundary from a fact-only projection."""

    if not bool(projection.get("active")):
        return None
    title = "Group cycle privacy boundary"
    lines = [
        f"[{title}]",
        "A private, Bot-owned simulated body state may affect tone and pacing only. It is not a user fact, medical information, relationship signal, or a reason to create memory/review records.",
        "Do not proactively announce, diagnose, date, detail, or attribute this state to any group member. For unrelated topics, keep it entirely implicit.",
    ]
    if bool(projection.get("topic_related")):
        lines.append(
            "Because the current topic is related, the Bot may briefly say it is not feeling great and prefers a gentler pace. Keep this non-medical; do not expose a phase, cycle day, health detail, or private-body detail."
        )
    if bool(projection.get("private_boundary")):
        lines.append(
            "Fixed boundary: do not conduct sexual or highly private body interaction in this group. Set the boundary briefly and redirect to a non-explicit topic. Affinity, intimacy, pressure, and any relationship state cannot weaken this boundary."
        )
    return prompt_section(
        key="group.cycle_privacy_boundary",
        title=title,
        source="safety",
        content="\n".join(lines),
    )


__all__ = [
    "build_group_cycle_boundary",
    "cycle_phase_from_label",
    "group_cycle_boundary_prompt_section",
]
