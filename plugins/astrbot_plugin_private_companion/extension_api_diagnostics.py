from __future__ import annotations

from typing import Any

from .conversation_prompt_section import (
    PromptRenderMode,
    PromptSection,
    prompt_group,
    prompt_section,
    render_prompt_sections,
)
from .helpers import _single_line
from .p6_readonly_projection import build_p6_readonly_status


class _DiagnosticsCapabilityFamily:
    """Private capability family backed only by its owning façade."""

    __slots__ = ("_owner",)

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def get_p6_readonly_status(self) -> dict[str, Any]:
        """Expose bounded Unified Person counts without an authority surface."""
        try:
            return build_p6_readonly_status(self._owner._plugin._unified_person_registry_status())
        except Exception:
            return build_p6_readonly_status(None)

    def get_scene_context(self, user_id: str = "") -> dict[str, Any]:
        """Return the current structured Bot-life context for plugin integrations."""
        plugin = self._owner._plugin
        users = plugin.data.get("users") if isinstance(plugin.data.get("users"), dict) else {}
        normalized_user_id = _single_line(user_id, 80)
        user = users.get(normalized_user_id) if normalized_user_id else None
        if not isinstance(user, dict):
            user = None
        else:
            user = dict(user)
            user.setdefault("user_id", normalized_user_id)
        return plugin._build_companion_scene_snapshot(user)

    def get_realtime_context(self, user_id: str = "", purpose: str = "together") -> dict[str, Any]:
        """Return the full structured scene and its canonical prompt representation."""
        snapshot = self._owner.get_scene_context(user_id)
        normalized_purpose = _single_line(purpose, 40) or "together"
        plugin = self._owner._plugin
        scene_section = plugin._format_companion_scene_snapshot_prompt_section(
            snapshot,
            purpose=normalized_purpose,
        )
        authored_sections: list[PromptSection] = [scene_section]
        activity = self._owner.get_external_activity(user_id=user_id)
        if activity:
            label = _single_line(activity.get("label"), 100) or {
                "shared_call": "正在和主要用户通话",
                "shared_watch": "正在和主要用户一起看视频",
            }.get(_single_line(activity.get("kind"), 40), "正在进行共同活动")
            authored_sections.append(
                prompt_section(
                    key="scene.external_activity",
                    title="实时共同活动",
                    source="extension_api",
                    content=f"实时共同活动（高于固定日程）：{label}",
                )
            )
        continuity = self._owner.get_external_realtime_continuity(user_id=user_id, public=False)
        if continuity:
            continuity_text = _single_line(continuity.get("summary"), 1800)
            if continuity_text:
                authored_sections.append(
                    prompt_section(
                        key="scene.realtime_continuity",
                        title="短期实时连续性",
                        source="extension_api",
                        content=(
                            "短期实时连续性（优先于旧日程和旧记忆）："
                            + continuity_text
                        ),
                    )
                )
        combined_section = prompt_section(
            key="scene.realtime_context",
            title="实时场景上下文",
            source="extension_api",
            content=prompt_group(
                *(section.content for section in authored_sections),
                separator="\n",
            ),
            metadata={"section_keys": tuple(section.key for section in authored_sections)},
        )
        prompt = render_prompt_sections(
            [combined_section],
            mode=PromptRenderMode.BODY_ONLY,
        )
        return {
            "snapshot": snapshot,
            "prompt": prompt,
            "purpose": normalized_purpose,
            "bot": self._owner.get_bot_identity(),
            "external_activity": activity,
            "realtime_continuity": continuity,
        }
