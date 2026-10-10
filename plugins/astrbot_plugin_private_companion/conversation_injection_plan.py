"""Request-scoped assembly plan for main-conversation prompt injections."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from astrbot.core.agent.message import TextPart

from .conversation_prompt_section import (
    ExactText,
    PromptCData,
    PromptField,
    PromptGroup,
    PromptList,
    PromptRenderMode,
    PromptSection,
    PromptTemplate,
    PromptText,
    XmlElement,
    exact_text,
    prompt_group,
    prompt_section,
    prompt_section_fingerprint,
    render_prompt_content,
    render_prompt_sections,
)
from .logging_util import get_module_logger


logger = get_module_logger(__name__)

PLAN_ATTR = "_private_companion_conversation_injection_plan"
TURN_FRAGMENTS_ATTR = "_private_companion_turn_prompt_fragments"
TURN_PLACEMENT_ATTR = "_private_companion_turn_prompt_placement"
TURN_PART_ATTR = "_private_companion_turn_fragments"
TURN_TEXT_ATTR = "_private_companion_conversation_plan_turn_text"
TURN_BASE_OVERRIDE_ATTR = "_private_companion_conversation_plan_turn_base_override"
DELIVERY_GROUP_MARKER_METADATA_KEY = "delivery_group_marker"

PLACEMENT_STABLE_SYSTEM = "stable_system"
PLACEMENT_DYNAMIC_SYSTEM = "dynamic_system"
PLACEMENT_TURN_TAIL = "turn_tail"
PLACEMENT_TOOL_CONTRACT = "tool_contract"
_PLACEMENTS = {
    PLACEMENT_STABLE_SYSTEM,
    PLACEMENT_DYNAMIC_SYSTEM,
    PLACEMENT_TURN_TAIL,
    PLACEMENT_TOOL_CONTRACT,
}
_MERGE_POLICIES = {"first", "replace", "append"}


def _clean_key(value: Any, fallback: str = "fragment") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:160] or fallback


_MISSING = object()


def _content_text(value: Any) -> str:
    if isinstance(value, ExactText):
        return value.text
    if isinstance(value, PromptSection):
        if isinstance(value.content, ExactText):
            return value.content.text
        return render_prompt_sections([value], mode=PromptRenderMode.BODY_ONLY)
    if isinstance(
        value,
        (
            PromptCData,
            PromptField,
            PromptGroup,
            PromptList,
            PromptTemplate,
            PromptText,
            XmlElement,
        ),
    ):
        return render_prompt_content(value, mode=PromptRenderMode.BODY_ONLY)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _content_bytes(value: Any) -> bytes:
    return _content_text(value).encode("utf-8", "ignore")


@dataclass
class ConversationInjectionBlock:
    section: PromptSection
    marker: str
    priority: int = 50
    placement: str = PLACEMENT_TURN_TAIL
    merge_policy: str = "first"
    materialized: bool = False
    index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.section.key

    @property
    def title(self) -> str:
        return self.section.title

    @property
    def source(self) -> str:
        return self.section.source

    @property
    def content(self) -> Any:
        content = self.section.content
        if self.section.children:
            return _content_text(self.section)
        if isinstance(
            content,
            (
                ExactText,
                PromptCData,
                PromptField,
                PromptGroup,
                PromptList,
                PromptTemplate,
                PromptText,
                XmlElement,
            ),
        ):
            return _content_text(content)
        return content

    def replace_section(
        self,
        *,
        content: Any = _MISSING,
        key: Any = _MISSING,
        title: Any = _MISSING,
        source: Any = _MISSING,
        children: Any = _MISSING,
    ) -> None:
        current = self.section
        self.section = prompt_section(
            key=current.key if key is _MISSING else key,
            title=current.title if title is _MISSING else title,
            source=current.source if source is _MISSING else source,
            content=current.content if content is _MISSING else content,
            children=current.children if children is _MISSING else children,
            metadata=current.metadata,
        )

    @staticmethod
    def _manifest_section(
        section: PromptSection,
        *,
        include_content: bool,
    ) -> dict[str, Any]:
        body = render_prompt_content(section.content, mode=PromptRenderMode.BODY_ONLY)
        item: dict[str, Any] = {
            "key": section.key,
            "source": section.source,
            "title": section.title,
            "chars": len(body),
            "sha256": hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest(),
            "metadata": copy.deepcopy(dict(section.metadata)),
            "children": [
                ConversationInjectionBlock._manifest_section(
                    child,
                    include_content=include_content,
                )
                for child in section.children
            ],
        }
        if include_content:
            item["content"] = body
        return item

    def manifest_item(self, *, include_content: bool = False) -> dict[str, Any]:
        authored_content = self.content
        item = {
            "key": self.key,
            "marker": self.marker,
            "source": self.source,
            "title": self.title,
            "priority": self.priority,
            "placement": self.placement,
            "merge_policy": self.merge_policy,
            "materialized": self.materialized,
            "index": self.index,
            "chars": len(_content_text(authored_content)),
            "sha256": hashlib.sha256(_content_bytes(authored_content)).hexdigest(),
            "metadata": copy.deepcopy(self.metadata),
            "children": [
                self._manifest_section(child, include_content=include_content)
                for child in self.section.children
            ],
            "conflicts": copy.deepcopy(self.conflicts),
        }
        if include_content:
            item["content"] = copy.deepcopy(self.content)
        return item


class ConversationInjectionPlan:
    """Own prompt blocks for one ProviderRequest and render them deterministically."""

    def __init__(self, *, strict_conflicts: bool = False) -> None:
        self._blocks: list[ConversationInjectionBlock] = []
        self._by_key: dict[str, ConversationInjectionBlock] = {}
        self._next_index = 0
        self._frozen = False
        self._prefer_extra_user_content = True
        self._strict_conflicts = bool(strict_conflicts)

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        self._frozen = True

    def add(
        self,
        *,
        section: PromptSection,
        marker: str = "",
        priority: int = 50,
        placement: str = PLACEMENT_TURN_TAIL,
        merge_policy: str = "first",
        materialized: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationInjectionBlock | None:
        if self._frozen:
            raise RuntimeError("conversation injection plan is frozen")
        normalized_marker = _clean_key(marker, "")
        if not isinstance(section, PromptSection):
            raise TypeError("ConversationInjectionPlan.add requires PromptSection")
        authored = section
        normalized_key = authored.key
        if not normalized_key or not authored.source:
            raise ValueError("conversation injection sections require key and source")
        normalized_placement = str(placement or "").strip().lower()
        if normalized_placement not in _PLACEMENTS:
            raise ValueError(f"unsupported conversation injection placement: {placement}")
        normalized_merge = str(merge_policy or "first").strip().lower()
        if normalized_merge not in _MERGE_POLICIES:
            raise ValueError(f"unsupported conversation injection merge policy: {merge_policy}")

        body = authored.content
        if (
            body is None
            or (isinstance(body, str) and not body.strip())
        ) and not (authored is not None and authored.children):
            return None
        if normalized_placement == PLACEMENT_TOOL_CONTRACT and not isinstance(body, ExactText):
            raise TypeError("tool contract placement requires ExactText content")

        existing = self._by_key.get(normalized_key)
        existing_by_marker = False
        if existing is None and normalized_marker:
            existing = next(
                (
                    block
                    for block in self._blocks
                    if block.marker == normalized_marker
                ),
                None,
            )
            existing_by_marker = existing is not None
        if existing is not None:
            old_fingerprint = prompt_section_fingerprint(existing.section)
            new_fingerprint = prompt_section_fingerprint(authored)
            if old_fingerprint == new_fingerprint:
                return existing
            if normalized_merge == "first":
                conflict = {
                    "kind": "key" if existing.key == normalized_key else "marker",
                    "key": normalized_key,
                    "marker": normalized_marker,
                    "existing_source": existing.source,
                    "incoming_source": authored.source,
                    "existing_sha256": old_fingerprint,
                    "incoming_sha256": new_fingerprint,
                }
                if self._strict_conflicts:
                    raise ValueError(
                        f"conversation prompt key conflict: {normalized_key}"
                    )
                existing.conflicts.append(conflict)
                logger.warning(
                    "主对话提示词 key 冲突,保留首个片段: key=%s existing=%s incoming=%s",
                    normalized_key,
                    existing.source,
                    authored.source,
                )
                return existing
            if normalized_merge == "append":
                existing_text = _content_text(existing.section.content)
                new_text = _content_text(authored.content)
                merged_content = existing.section.content
                if new_text != existing_text and new_text not in existing_text.split("\n\n"):
                    if isinstance(existing.section.content, ExactText) or isinstance(authored.content, ExactText):
                        merged_content = exact_text(existing_text + new_text)
                    else:
                        merged_content = prompt_group(
                            existing.section.content,
                            authored.content,
                            separator="\n\n",
                        )
                merged_children = (*existing.section.children, *authored.children)
                if merged_content is not existing.section.content or merged_children != existing.section.children:
                    existing.replace_section(
                        content=merged_content,
                        children=merged_children,
                    )
                if metadata:
                    existing.metadata.update(copy.deepcopy(metadata))
                return existing
            old_key = existing.key
            existing.marker = normalized_marker
            existing.section = authored
            existing.priority = int(priority)
            existing.placement = normalized_placement
            existing.merge_policy = normalized_merge
            existing.materialized = bool(materialized)
            existing.metadata = copy.deepcopy(metadata or {})
            existing.conflicts = []
            if existing_by_marker and old_key != normalized_key:
                self._by_key.pop(old_key, None)
                self._by_key[normalized_key] = existing
            return existing

        block = ConversationInjectionBlock(
            section=authored,
            marker=normalized_marker,
            priority=int(priority),
            placement=normalized_placement,
            merge_policy=normalized_merge,
            materialized=bool(materialized),
            index=self._next_index,
            metadata=copy.deepcopy(metadata or {}),
        )
        self._next_index += 1
        self._blocks.append(block)
        self._by_key[normalized_key] = block
        return block

    def annotate_marker(
        self,
        marker: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        marker_text = _clean_key(marker, "")
        target = next((block for block in self._blocks if block.marker == marker_text), None)
        if target is None:
            return False
        if metadata:
            target.metadata.update(copy.deepcopy(metadata))
        return True

    def materialize_system_block(
        self,
        req: Any,
        *,
        section: PromptSection,
        marker: str = "",
        priority: int = 50,
        placement: str = PLACEMENT_DYNAMIC_SYSTEM,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Append one system block in legacy order while registering its provenance."""

        if not isinstance(section, PromptSection):
            raise TypeError("materialize_system_block requires PromptSection")
        existing = self._by_key.get(section.key)
        if existing is None and marker:
            existing = next(
                (block for block in self._blocks if block.marker == _clean_key(marker, "")),
                None,
            )
        common = {
            "marker": _clean_key(marker, ""),
            "priority": priority,
            "placement": placement,
            "materialized": True,
            "metadata": {"materialized_by_plan": True, **copy.deepcopy(metadata or {})},
        }
        block = self.add(section=section, **common)
        if block is None or block is existing:
            return False
        self._render_system(req)
        return True

    def contains_key(self, key: str) -> bool:
        return key in self._by_key

    def contains_marker(self, marker: str) -> bool:
        marker_text = _clean_key(marker, "")
        return bool(marker_text) and any(block.marker == marker_text for block in self._blocks)

    def remove_markers(self, markers: Iterable[str]) -> int:
        """Remove owned blocks before the plan is frozen for a narrower scope."""
        if self._frozen:
            raise RuntimeError("conversation injection plan is frozen")
        wanted = {_clean_key(marker, "") for marker in markers}
        wanted.discard("")
        removed_ids = {
            id(block)
            for block in self._blocks
            if block.marker in wanted
            or _clean_key(
                block.metadata.get(DELIVERY_GROUP_MARKER_METADATA_KEY),
                "",
            )
            in wanted
        }
        if not removed_ids:
            return 0
        self._blocks = [block for block in self._blocks if id(block) not in removed_ids]
        self._by_key = {
            key: block
            for key, block in self._by_key.items()
            if id(block) not in removed_ids
        }
        return len(removed_ids)

    def blocks(self, *, placement: str | None = None, include_materialized: bool = True) -> list[ConversationInjectionBlock]:
        selected = self._blocks
        if placement is not None:
            selected = [block for block in selected if block.placement == placement]
        if not include_materialized:
            selected = [block for block in selected if not block.materialized]
        return sorted(selected, key=lambda block: (block.priority, block.index))

    def turn_fragments(self) -> list[dict[str, Any]]:
        fragments: list[dict[str, Any]] = []
        seen_markers: set[str] = set()
        for block in self.blocks(placement=PLACEMENT_TURN_TAIL, include_materialized=False):
            visible_content = self._visible_content(block)
            if not block.marker or not visible_content:
                continue
            if block.marker in seen_markers:
                continue
            seen_markers.add(block.marker)
            fragments.append(
                {
                    "marker": block.marker,
                    "content": visible_content,
                    "priority": block.priority,
                    "source": block.source,
                    "index": block.index,
                }
            )
        return fragments

    def manifest(self, *, include_content: bool = False) -> list[dict[str, Any]]:
        return [
            block.manifest_item(include_content=include_content)
            for block in self.blocks()
        ]

    @staticmethod
    def _visible_content(block: ConversationInjectionBlock) -> str:
        if isinstance(block.section.content, ExactText) or block.placement == PLACEMENT_TOOL_CONTRACT:
            return _content_text(block.section.content)
        return render_prompt_sections([block.section])

    @staticmethod
    def _remove_once(text: str, candidate: str) -> str:
        if not candidate or candidate not in text:
            return text
        cleaned = text.replace(candidate, "", 1)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    def _strip_registered_materialized_blocks(self, current: str) -> str:
        """Remove legacy/raw copies before rendering owned system placements."""

        result = current
        for block in self.blocks(include_materialized=True):
            if (
                not block.materialized
                or isinstance(block.section.content, ExactText)
                or block.placement == PLACEMENT_TOOL_CONTRACT
            ):
                continue
            content = _content_text(block.section)
            raw = f"{block.marker}\n{content}".strip() if block.marker else content
            visible = self._visible_content(block)
            rendered = f"{block.marker}\n{visible}".strip() if block.marker else visible
            for candidate in (rendered, raw, visible):
                before = result
                result = self._remove_once(result, candidate)
                if result != before:
                    break
        return result

    @staticmethod
    def _render_block_group(blocks: Iterable[ConversationInjectionBlock]) -> str:
        """Render typed sections in batches, flushing only exact contracts."""

        parts: list[str] = []
        pending: list[PromptSection] = []

        def flush_sections() -> None:
            if not pending:
                return
            parts.append(render_prompt_sections(pending))
            pending.clear()

        for block in blocks:
            if block.placement == PLACEMENT_TOOL_CONTRACT:
                continue
            if isinstance(block.section.content, ExactText):
                flush_sections()
                parts.append(_content_text(block.section.content))
                continue
            pending.append(block.section)
        flush_sections()
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def _remove_owned_turn_part(req: Any) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not isinstance(parts, list):
            return
        parts[:] = [
            part
            for part in parts
            if not bool(getattr(part, TURN_PART_ATTR, False))
        ]

    def _base_prompt(self, req: Any) -> str:
        override = getattr(req, TURN_BASE_OVERRIDE_ATTR, _MISSING)
        if override is not _MISSING:
            try:
                delattr(req, TURN_BASE_OVERRIDE_ATTR)
            except (AttributeError, TypeError):
                pass
            return str(override or "").strip()
        current = str(getattr(req, "prompt", "") or "")
        previous = str(getattr(req, TURN_TEXT_ATTR, "") or "")
        if previous:
            if current == previous:
                current = ""
            elif current.endswith(f"\n\n{previous}"):
                current = current[: -(len(previous) + 2)].rstrip()
            elif previous in current:
                current = self._remove_once(current, previous)
        return current.strip()

    def _render_system(self, req: Any) -> None:
        current = str(getattr(req, "system_prompt", "") or "")
        previous = str(getattr(req, "_private_companion_conversation_plan_system_text", "") or "")
        if previous:
            if current == previous:
                current = ""
            elif current.endswith(f"\n\n{previous}"):
                current = current[: -(len(previous) + 2)].rstrip()
            elif previous in current:
                current = self._remove_once(current, previous)
        current = self._strip_registered_materialized_blocks(current)
        rendered: list[str] = []
        for placement in (PLACEMENT_STABLE_SYSTEM, PLACEMENT_DYNAMIC_SYSTEM):
            placement_text = self._render_block_group(self.blocks(placement=placement))
            if placement_text:
                rendered.append(placement_text)
        owned = "\n\n".join(part for part in rendered if part)
        setattr(req, "_private_companion_conversation_plan_system_text", owned)
        if owned:
            req.system_prompt = f"{current}\n\n{owned}" if current else owned
        else:
            req.system_prompt = current

    def render_into(self, req: Any, *, prefer_extra_user_content: bool | None = None) -> str:
        if prefer_extra_user_content is not None:
            self._prefer_extra_user_content = bool(prefer_extra_user_content)
        self._render_system(req)
        base = self._base_prompt(req)
        fragments = self.turn_fragments()
        setattr(req, TURN_FRAGMENTS_ATTR, copy.deepcopy(fragments))
        if not fragments:
            req.prompt = base
            self._remove_owned_turn_part(req)
            setattr(req, TURN_TEXT_ATTR, "")
            setattr(req, TURN_PLACEMENT_ATTR, "none")
            return "none"

        turn_text = self._render_block_group(
            self.blocks(placement=PLACEMENT_TURN_TAIL, include_materialized=False)
        )
        managed = turn_text
        setattr(req, TURN_TEXT_ATTR, managed)
        if self._prefer_extra_user_content:
            req.prompt = base
            self._remove_owned_turn_part(req)
            try:
                if not isinstance(getattr(req, "extra_user_content_parts", None), list):
                    req.extra_user_content_parts = []
                part = TextPart(text=managed)
                marker = getattr(part, "mark_as_temp", None)
                if callable(marker):
                    part = marker()
                try:
                    setattr(part, TURN_PART_ATTR, True)
                except Exception:
                    pass
                req.extra_user_content_parts.append(part)
                setattr(req, TURN_PLACEMENT_ATTR, "extra_user_content_parts")
                return "extra_user_content_parts"
            except Exception:
                self._prefer_extra_user_content = False

        req.prompt = f"{base}\n\n{managed}" if base else managed
        self._remove_owned_turn_part(req)
        setattr(req, TURN_PLACEMENT_ATTR, "prompt")
        return "prompt"


def get_conversation_injection_plan(req: Any, *, create: bool = True) -> ConversationInjectionPlan | None:
    plan = getattr(req, PLAN_ATTR, None)
    if isinstance(plan, ConversationInjectionPlan):
        return plan
    if not create or req is None:
        return None
    plan = ConversationInjectionPlan()
    setattr(req, PLAN_ATTR, plan)
    return plan


def replace_conversation_turn_prompt(req: Any, prompt: str) -> bool:
    """Replace the request's user-authored turn text through the canonical renderer."""
    if req is None or not isinstance(prompt, str):
        return False
    plan = get_conversation_injection_plan(req)
    if plan is None:
        return False
    setattr(req, TURN_BASE_OVERRIDE_ATTR, prompt)
    plan.render_into(req)
    return True


__all__ = [
    "ConversationInjectionBlock",
    "ConversationInjectionPlan",
    "DELIVERY_GROUP_MARKER_METADATA_KEY",
    "PLACEMENT_DYNAMIC_SYSTEM",
    "PLACEMENT_STABLE_SYSTEM",
    "PLACEMENT_TOOL_CONTRACT",
    "PLACEMENT_TURN_TAIL",
    "PLAN_ATTR",
    "get_conversation_injection_plan",
    "replace_conversation_turn_prompt",
]
