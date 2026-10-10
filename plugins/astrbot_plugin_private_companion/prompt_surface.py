# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .conversation_prompt_section import (
    PromptSection,
    prompt_group,
    prompt_section,
    prompt_section_fingerprint,
)
from .logging_util import get_module_logger


logger = get_module_logger(__name__)


@dataclass(frozen=True, slots=True)
class PromptFragment:
    """One authored section plus surface-local ordering metadata."""

    section: PromptSection
    priority: int = 100
    index: int = 0

    @property
    def key(self) -> str:
        return self.section.key

    @property
    def content(self) -> Any:
        return self.section.content

    @property
    def title(self) -> str:
        return self.section.title

    @property
    def source(self) -> str:
        return self.section.source

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self.section.metadata)

    def normalized_key(self) -> str:
        return self.section.key


@dataclass(frozen=True, slots=True)
class CollectedPromptContext:
    """Typed result from one bounded asynchronous prompt collector."""

    key: str
    priority: int
    sections: tuple[PromptSection, ...]
    status: str
    metadata: dict[str, Any]


class PromptSurface:
    """Collect authored sections before request-level placement and rendering."""

    def __init__(self, *, strict_conflicts: bool = False) -> None:
        self._fragments: list[PromptFragment] = []
        self._by_key: dict[str, PromptFragment] = {}
        self._conflicts: list[dict[str, object]] = []
        self._next_index = 0
        self._strict_conflicts = bool(strict_conflicts)

    def add(
        self,
        section: PromptSection,
        *,
        priority: int = 100,
        merge_policy: str = "first",
    ) -> PromptFragment | None:
        """Add one fully authored section."""

        if not isinstance(section, PromptSection):
            raise TypeError("PromptSurface.add requires PromptSection")
        if not section.key or not section.source:
            raise ValueError("PromptSurface requires a section with key and source")
        if (
            section.content is None
            or (isinstance(section.content, str) and not section.content.strip())
        ) and not section.children:
            return None
        policy = str(merge_policy or "first").strip().lower()
        if policy not in {"first", "replace", "append"}:
            raise ValueError(f"unsupported prompt surface merge policy: {merge_policy}")
        existing = self._by_key.get(section.key)
        if existing is not None:
            old_fingerprint = prompt_section_fingerprint(existing.section)
            new_fingerprint = prompt_section_fingerprint(section)
            if old_fingerprint == new_fingerprint:
                return existing
            if policy == "first":
                conflict = {
                    "key": section.key,
                    "existing_source": existing.section.source,
                    "incoming_source": section.source,
                    "existing_sha256": old_fingerprint,
                    "incoming_sha256": new_fingerprint,
                }
                if self._strict_conflicts:
                    raise ValueError(f"prompt surface key conflict: {section.key}")
                self._conflicts.append(conflict)
                logger.warning(
                    "PromptSurface key 冲突,保留首个片段: key=%s existing=%s incoming=%s",
                    section.key,
                    existing.section.source,
                    section.source,
                )
                return existing
            if policy == "replace":
                replacement = PromptFragment(
                    section=section,
                    priority=int(priority),
                    index=existing.index,
                )
                self._fragments[self._fragments.index(existing)] = replacement
                self._by_key[section.key] = replacement
                return replacement
            merged_content = existing.section.content
            if existing.section.content != section.content:
                merged_content = prompt_group(
                    existing.section.content,
                    section.content,
                    separator="\n\n",
                )
            merged = prompt_section(
                key=existing.section.key,
                title=existing.section.title,
                source=existing.section.source,
                content=merged_content,
                children=(*existing.section.children, *section.children),
                metadata=existing.section.metadata,
            )
            replacement = PromptFragment(
                section=merged,
                priority=existing.priority,
                index=existing.index,
            )
            self._fragments[self._fragments.index(existing)] = replacement
            self._by_key[section.key] = replacement
            return replacement
        fragment = PromptFragment(
            section=section,
            priority=int(priority),
            index=self._next_index,
        )
        self._fragments.append(fragment)
        self._by_key[section.key] = fragment
        self._next_index += 1
        return fragment

    def extend(self, fragments: Iterable[PromptFragment | PromptSection]) -> None:
        for fragment in fragments:
            if isinstance(fragment, PromptFragment):
                self.add(fragment.section, priority=fragment.priority)
            elif isinstance(fragment, PromptSection):
                self.add(fragment)
            else:
                raise TypeError("PromptSurface.extend requires PromptFragment or PromptSection values")

    def fragments(self) -> tuple[PromptFragment, ...]:
        return tuple(sorted(self._fragments, key=lambda item: (item.priority, item.index)))

    def sections(self) -> tuple[PromptSection, ...]:
        return tuple(fragment.section for fragment in self.fragments())

    def conflicts(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._conflicts)

    def partition_sections(
        self,
        predicate: Callable[[PromptFragment], bool],
    ) -> tuple[tuple[PromptSection, ...], tuple[PromptSection, ...]]:
        matched: list[PromptSection] = []
        rest: list[PromptSection] = []
        for fragment in self.fragments():
            (matched if predicate(fragment) else rest).append(fragment.section)
        return tuple(matched), tuple(rest)

    def __len__(self) -> int:
        return len(self._fragments)
