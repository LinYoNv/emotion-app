from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from . import photo_wardrobe_decision as _wardrobe_rules
from .conversation_prompt_section import (
    PhotoPromptContent,
    PromptRenderMode,
    PromptSection,
    exact_text,
    prompt_section,
    render_prompt_sections,
)


_SANITIZER_VERSION = 3
_SECTION_SOURCES = frozenset(
    {
        "user_request",
        "visual_memory",
        "scene_context",
        "preset",
        "fixed_prompt",
        "recent_continuity",
        "wardrobe_decision",
        "managed_reference",
        "reference_fallback",
        "composition",
        "edit_contract",
    }
)
_SELFIE_WORKFLOWS = frozenset({"selfie", "portrait", "自拍", "人像"})
_EDIT_WORKFLOWS = frozenset({"edit", "改图", "修图", "重绘", "p图"})
_GENERIC_WARDROBE_PATTERN = re.compile(
    r"衣服|服装|衣着|穿搭|配饰|服饰|着装|原来的衣服|参考图中的衣服|"
    r"\b(?:clothes|clothing|outfit|wardrobe|attire|garment|accessor(?:y|ies))\b",
    flags=re.I,
)
_DAILY_OUTFIT_PATTERN = re.compile(
    r"(?:今日穿搭|当天穿搭|日常穿搭|today'?s outfit|daily outfit)",
    flags=re.I,
)
_RECENT_OUTFIT_CONTINUITY_PATTERN = re.compile(
    r"(?:,\s*)?(?:(?:preserve\s+)?(?:the\s+)?)?exact\s+outfit\s+and\s+accessories"
    r"|(?:，\s*)?(?:精确保留|保持|延续)?(?:参考图(?:中|里的)?)?(?:完整|原有|相同)?服装(?:和|与)配饰",
    flags=re.I,
)
_SPECIFIC_OUTFIT_ITEM_PATTERN = re.compile(
    r"连衣裙|裙子|短裙|长裙|吊带|衬衫|外套|夹克|西装|制服|汉服|旗袍|和服|洛丽塔|"
    r"裤子|裤|毛衣|卫衣|T恤|背心|上衣|套装|袜子|袜|鞋子|鞋|"
    r"\b(?:dress|skirt|shirt|blouse|coat|jacket|suit|uniform|hoodie|sweater|pants|trousers|shorts|top)\b",
    flags=re.I,
)
_EMBEDDED_NEGATION_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|not|avoid|without|no|exclude|skip|remove)\s+"
    r"|(?:不要|避免|禁止|不许|不得|别(?:再)?)(?:穿|用|使用|选)?\s*",
    flags=re.I,
)
_NEGATIVE_TO_POSITIVE_TRANSITION_PATTERN = re.compile(
    r"\b(?:but|instead|however)\b\s*"
    r"|(?:但|不过|可是|而是)?(?:改穿|换成|换上|换为|改为|要穿|穿上)\s*",
    flags=re.I,
)
_OUTFIT_ROTATION_NEGATIVE_PATTERN = re.compile(
    r"(?:\b(?:same|repeat(?:ed|ing)?|reus(?:e|ed|ing)|recent(?:ly)?|previous(?:ly)?|"
    r"histor(?:y|ical)|rotation|duplicate)\b.{0,80}\b(?:outfits?|wardrobes?|clothes|clothing|attire)\b"
    r"|\b(?:outfits?|wardrobes?|clothes|clothing|attire)\b.{0,80}\b(?:same|repeat(?:ed|ing)?|"
    r"reus(?:e|ed|ing)|recent(?:ly)?|previous(?:ly)?|histor(?:y|ical)|rotation|duplicate)\b"
    r"|(?:重复|复用|沿用|相同|同一|最近|近期|历史|此前|之前|上一套|用过|穿过|轮换|轮替)"
    r".{0,40}(?:穿搭|衣服|服装|衣着|着装|配饰)"
    r"|(?:穿搭|衣服|服装|衣着|着装|配饰).{0,40}"
    r"(?:重复|复用|沿用|相同|同一|最近|近期|历史|此前|之前|上一套|用过|穿过|轮换|轮替))",
    flags=re.I,
)

__all__ = [
    "ResolvedPhotoPromptContext",
    "compile_local_photo_prompt",
    "resolve_photo_prompt_context",
]


def _photo_content(value: Any) -> PhotoPromptContent | None:
    if not isinstance(value, PromptSection) or value.children:
        return None
    payload = value.content
    if not isinstance(payload, PhotoPromptContent):
        return None
    if payload.domain_source not in _SECTION_SOURCES:
        return None
    return payload


def _validated_photo_prompt_section(value: Any) -> PromptSection | None:
    """Accept only canonical sections carrying typed photo-domain content."""

    return value if _photo_content(value) is not None else None


_MISSING = object()


def _replace_photo_prompt_section(
    section: PromptSection,
    *,
    positive: Any = _MISSING,
    negative: Any = _MISSING,
) -> PromptSection:
    payload = _photo_content(section)
    if payload is None:
        raise TypeError("section must carry PhotoPromptContent")
    updates: dict[str, Any] = {}
    if positive is not _MISSING:
        updates["positive"] = positive
    if negative is not _MISSING:
        updates["negative"] = negative
    return replace(section, content=replace(payload, **updates))


def _photo_prompt_section_payload(value: Any) -> dict[str, Any]:
    """Serialize one photo section to the stable external six-field contract."""

    section = _validated_photo_prompt_section(value)
    if section is None:
        raise TypeError("value is not a compatible photo prompt section")
    payload = _photo_content(section)
    if payload is None:
        raise TypeError("value is not a compatible photo prompt section")
    return {
        "name": section.title,
        "source": payload.domain_source,
        "positive": payload.positive,
        "negative": payload.negative,
        "protected": payload.protected,
        "sanitize_conflicts": payload.sanitize_conflicts,
    }


def _section_conflict_sanitization_enabled(section: PromptSection) -> bool:
    payload = _photo_content(section)
    if payload is None:
        raise TypeError("section must carry PhotoPromptContent")
    if payload.sanitize_conflicts is not None:
        return payload.sanitize_conflicts
    return not payload.protected


@dataclass(frozen=True, slots=True)
class ResolvedPhotoPromptContext:
    final_prompt: str
    complete_prompt: str
    prompt_sections: tuple[PromptSection, ...]
    reference: Any
    detected_conflicts: tuple[dict[str, Any], ...]
    removed_conflicts: tuple[dict[str, Any], ...]
    residual_conflicts: tuple[dict[str, Any], ...]
    reference_removed: dict[str, Any] | None
    sanitizer_version: int = _SANITIZER_VERSION


def _value(subject: Any, name: str, default: Any = None) -> Any:
    if isinstance(subject, Mapping):
        return subject.get(name, default)
    return getattr(subject, name, default)


_CLIP_BOUNDARY_CHARS = frozenset(" \t\r\n,.;:!?，。；：！？")
_CLIP_BOUNDARY_PATTERN = re.compile(r"[\s,.;:!?，。；：！？]+")
_COMPACTED_MARKER = " ... [section compacted] ... "
_NEGATIVE_LABEL_MAX_WORDS = 6


def _clip_prefix_at_boundary(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    candidate = text[:limit]
    if len(text) <= limit or text[limit] in _CLIP_BOUNDARY_CHARS:
        return candidate.rstrip()
    matches = list(_CLIP_BOUNDARY_PATTERN.finditer(candidate))
    return candidate[: matches[-1].end()].rstrip() if matches else ""


def _clip_tail_at_boundary(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    start = max(0, len(text) - limit)
    candidate = text[start:]
    if start == 0 or text[start - 1] in _CLIP_BOUNDARY_CHARS:
        return candidate.lstrip()
    match = _CLIP_BOUNDARY_PATTERN.search(candidate)
    return candidate[match.end():].lstrip() if match else ""


def _clip(value: Any, limit: int, *, preserve_tail: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if limit <= 0 or len(text) <= limit:
        return text
    marker = _COMPACTED_MARKER
    if not preserve_tail or limit <= len(marker) + 40:
        return _clip_prefix_at_boundary(text, limit)
    available = limit - len(marker)
    head_size = max(20, int(available * 0.62))
    tail_size = max(20, available - head_size)
    head = _clip_prefix_at_boundary(text, head_size)
    tail = _clip_tail_at_boundary(text, tail_size)
    if head and tail:
        return f"{head}{marker}{tail}"
    return head or tail


def _categories(value: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            category
            for category, _start, _end, _matched in _wardrobe_rules._outfit_category_matches(value)
        )
    )


def _compatible(category: str, active_category: str) -> bool:
    return category == active_category


def _split_embedded_polarity(text: str) -> tuple[str, str]:
    raw = str(text or "").strip()
    if not raw or not _EMBEDDED_NEGATION_PATTERN.search(raw):
        return raw, ""

    positive_parts: list[str] = []
    negative_parts: list[str] = []
    sentences = re.split(r"[;；.!?。！？]+", raw)
    for sentence in sentences:
        polarity = "positive"
        for comma_part in re.split(r"[,，]+", sentence):
            remaining = comma_part.strip()
            while remaining:
                if polarity == "positive":
                    marker = _EMBEDDED_NEGATION_PATTERN.search(remaining)
                    if marker is None:
                        positive_parts.append(remaining)
                        break
                    before = remaining[:marker.start()].strip()
                    if before:
                        positive_parts.append(before)
                    remaining = remaining[marker.end():].strip()
                    polarity = "negative"
                    continue

                transition = _NEGATIVE_TO_POSITIVE_TRANSITION_PATTERN.search(remaining)
                if transition is None:
                    negative_parts.append(remaining)
                    break
                before = remaining[:transition.start()].strip()
                if before:
                    negative_parts.append(before)
                remaining = remaining[transition.end():].strip()
                polarity = "positive"

    return ", ".join(positive_parts), ", ".join(negative_parts)


def _excluded_outfit_terms(wardrobe: Any) -> tuple[str, ...]:
    raw = _wardrobe_rules._clean_text(_value(wardrobe, "excluded_outfit_text", ""), 1200)
    terms: list[str] = []
    for clause in re.split(r"(?:\r?\n+|[；;，,。]+)", raw):
        cleaned = _wardrobe_rules._clean_text(clause, 240).strip(" ,.;；。，")
        if not cleaned:
            continue
        _negative, content = _wardrobe_rules._negative_clause_content(cleaned)
        term = _wardrobe_rules._clean_text(content or cleaned, 160).lower()
        if term:
            terms.append(term)
    return tuple(dict.fromkeys(terms))


def _specific_outfit_items(value: Any) -> frozenset[str]:
    normalized = _wardrobe_rules._clean_text(value, 1200).lower()
    return frozenset(match.group(0).lower() for match in _SPECIFIC_OUTFIT_ITEM_PATTERN.finditer(normalized))


def _generic_wardrobe_is_compatible(text: str, active_category: str) -> bool:
    normalized = _wardrobe_rules._clean_text(text, 4000).lower()
    if re.search(
        r"\b(?:one|single)\s+(?:single\s+)?coherent\s+outfit\b"
        r"|\bthe\s+same\s+outfit\b"
        r"|\b(?:requested|authoritative|resolved)\s+(?:wardrobe|outfit)\b"
        r"|\b(?:current|selected|established|stable|consistent)\s+(?:wardrobe|outfit|attire)\b"
        r"|\bwardrobe\s+(?:decision|ruling)\b"
        r"|\bconflicting\s+(?:schedule\s+location\s+or\s+)?wardrobe\b",
        normalized,
        flags=re.I,
    ):
        return True
    return active_category == "reference_outfit" and bool(
        re.search(r"\bexact\s+outfit\s+and\s+accessories\b", normalized, flags=re.I)
    )


def _preview(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"(?i)data:image/[^;\s]+;base64,[a-z0-9+/=]+", "[image-data]", text)
    text = re.sub(
        r"(?i)(?:[a-z]:\\[^,;\r\n]+|/(?:[^/\r\n,;]+/)+[^,;\r\n]*)",
        "[path]",
        text,
    )
    return text[:120]


def _audit(
    section: PromptSection,
    *,
    rule: str,
    category: str,
    action: str,
    text: str,
) -> dict[str, Any]:
    raw = str(text or "")
    payload = _photo_content(section)
    if payload is None:
        raise TypeError("section must carry PhotoPromptContent")
    return {
        "source": payload.domain_source,
        "section": section.title,
        "rule": rule,
        "category": category,
        "action": action,
        "preview": _preview(raw),
        "sha256": hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest(),
    }


def _positive_conflict(
    text: str,
    *,
    active_category: str,
    authoritative: bool,
    excluded_categories: frozenset[str],
    excluded_outfit_terms: tuple[str, ...],
) -> tuple[str, str] | None:
    categories = _categories(text)
    daily = bool(_DAILY_OUTFIT_PATTERN.search(text))
    if authoritative:
        if daily and active_category != "daily_outfit":
            return "daily_outfit_conflict", "daily_outfit"
        incompatible = next(
            (category for category in categories if not _compatible(category, active_category)),
            "",
        )
        if incompatible:
            return "incompatible_wardrobe", incompatible
        if (
            not categories
            and not _generic_wardrobe_is_compatible(text, active_category)
            and (
                _wardrobe_rules._contains_specific_outfit_text(text)
                or _GENERIC_WARDROBE_PATTERN.search(text)
            )
        ):
            return "unverified_wardrobe", "unknown"
    excluded = next((category for category in categories if category in excluded_categories), "")
    if excluded:
        return "excluded_wardrobe", excluded
    normalized = _wardrobe_rules._clean_text(text, 4000).lower()
    if next((term for term in excluded_outfit_terms if term in normalized), ""):
        return "excluded_outfit_item", "specific_outfit"
    return None


def _negative_conflict(
    text: str,
    *,
    active_category: str,
    authoritative: bool,
    authoritative_items: frozenset[str],
) -> tuple[str, str] | None:
    if not authoritative:
        return None
    # Rotation/history exclusions constrain reuse; they do not reject the
    # authoritative outfit category or an item that merely appears in history.
    if _OUTFIT_ROTATION_NEGATIVE_PATTERN.search(str(text or "")):
        return None
    categories = _categories(text)
    denied = next((category for category in categories if _compatible(category, active_category)), "")
    if denied:
        explicit_negation, _ = _wardrobe_rules._negative_clause_content(text)
        label_like = len(str(text).strip().split()) <= _NEGATIVE_LABEL_MAX_WORDS
        if explicit_negation or label_like:
            return "authoritative_wardrobe_negated", denied
    denied_item = next(
        (item for item in _specific_outfit_items(text) if item in authoritative_items),
        "",
    )
    if denied_item:
        return "authoritative_outfit_item_negated", denied_item
    return None


def _split_clauses(text: str) -> tuple[list[str], str]:
    raw = str(text or "").strip()
    if not raw:
        return [], "；"
    structural = [
        item.strip()
        for item in re.split(r"(?:\r?\n+|[；;]+|(?<=[.!?。！？])\s+)", raw)
        if item.strip()
    ]
    if len(structural) > 1:
        return structural, "；"
    comma_parts = [item.strip() for item in re.split(r"[,，]+", raw) if item.strip()]
    if len(comma_parts) > 1:
        return comma_parts, ", "
    return [raw], "；"


def _sanitize_field(
    section: PromptSection,
    text: str,
    *,
    negative: bool,
    active_category: str,
    authoritative: bool,
    authoritative_items: frozenset[str],
    excluded_categories: frozenset[str],
    excluded_outfit_terms: tuple[str, ...],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    if not text:
        return "", [], []
    clauses, separator = _split_clauses(text)
    conflicts: list[dict[str, Any]] = []
    removals: list[dict[str, Any]] = []
    kept: list[str] = []
    for clause in clauses:
        conflict = (
            _negative_conflict(
                clause,
                active_category=active_category,
                authoritative=authoritative,
                authoritative_items=authoritative_items,
            )
            if negative
            else _positive_conflict(
                clause,
                active_category=active_category,
                authoritative=authoritative,
                excluded_categories=excluded_categories,
                excluded_outfit_terms=excluded_outfit_terms,
            )
        )
        if conflict is None:
            kept.append(clause)
            continue
        rule, category = conflict
        conflicts.append(
            _audit(section, rule=rule, category=category, action="detected", text=clause)
        )
        action = (
            "section_dropped"
            if len(clauses) == 1 and rule == "unverified_wardrobe"
            else "clause_removed"
        )
        removals.append(
            _audit(section, rule=rule, category=category, action=action, text=clause)
        )
    return separator.join(kept), conflicts, removals


def _conflicts_in_section(
    section: PromptSection,
    *,
    active_category: str,
    authoritative: bool,
    authoritative_items: frozenset[str],
    excluded_categories: frozenset[str],
    excluded_outfit_terms: tuple[str, ...],
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    payload = _photo_content(section)
    if payload is None:
        raise TypeError("section must carry PhotoPromptContent")
    for text, negative in ((payload.positive, False), (payload.negative, True)):
        conflict = (
            _negative_conflict(
                text,
                active_category=active_category,
                authoritative=authoritative,
                authoritative_items=authoritative_items,
            )
            if negative
            else _positive_conflict(
                text,
                active_category=active_category,
                authoritative=authoritative,
                excluded_categories=excluded_categories,
                excluded_outfit_terms=excluded_outfit_terms,
            )
        )
        if conflict:
            rule, category = conflict
            found.append(
                _audit(section, rule=rule, category=category, action="residual", text=text)
            )
    return found


def _sanitize_sections(
    sections: Sequence[PromptSection],
    wardrobe: Any,
) -> tuple[
    list[PromptSection],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    active_category = str(_value(wardrobe, "category", "") or "").strip().lower()
    authoritative = bool(_value(wardrobe, "lock_outfit", False) and active_category)
    excluded_categories = frozenset(
        str(item or "").strip().lower()
        for item in (_value(wardrobe, "excluded_categories", ()) or ())
        if str(item or "").strip()
    )
    excluded_outfit_terms = _excluded_outfit_terms(wardrobe)
    authoritative_items = _specific_outfit_items(_value(wardrobe, "requested_outfit_text", ""))
    sanitized: list[PromptSection] = []
    detected: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    effective_roles = frozenset(
        str(item or "").strip().lower()
        for item in (_value(wardrobe, "effective_reference_roles", ()) or ())
        if str(item or "").strip()
    )
    for section in sections:
        payload = _photo_content(section)
        if payload is None:
            raise TypeError("sections must contain PromptSection values with PhotoPromptContent")
        if (
            not _section_conflict_sanitization_enabled(section)
            or payload.domain_source in {"user_request", "wardrobe_decision"}
        ):
            sanitized.append(section)
            continue
        if section.title != "global_fixed_prompt":
            positive_text, embedded_negative = _split_embedded_polarity(payload.positive)
            if embedded_negative:
                section = _replace_photo_prompt_section(
                    section,
                    positive=positive_text,
                    negative=", ".join(
                        part
                        for part in (payload.negative.strip(), embedded_negative)
                        if part
                    ),
                )
                payload = _photo_content(section)
                if payload is None:
                    raise AssertionError("typed photo section replacement lost its payload")
        if payload.domain_source == "recent_continuity" and "outfit" not in effective_roles:
            match = _RECENT_OUTFIT_CONTINUITY_PATTERN.search(payload.positive)
            if match:
                detected.append(
                    _audit(
                        section,
                        rule="inactive_reference_outfit_role",
                        category="reference_outfit",
                        action="detected",
                        text=match.group(0),
                    )
                )
                removed.append(
                    _audit(
                        section,
                        rule="inactive_reference_outfit_role",
                        category="reference_outfit",
                        action="clause_rewritten",
                        text=match.group(0),
                    )
                )
                rewritten = _RECENT_OUTFIT_CONTINUITY_PATTERN.sub("", payload.positive)
                rewritten = re.sub(r"\s+,", ",", rewritten)
                rewritten = re.sub(r",\s*([.;。；])", r"\1", rewritten)
                section = _replace_photo_prompt_section(
                    section,
                    positive=rewritten.strip(),
                )
                payload = _photo_content(section)
                if payload is None:
                    raise AssertionError("typed photo section replacement lost its payload")
        positive, positive_found, positive_removed = _sanitize_field(
            section,
            payload.positive,
            negative=False,
            active_category=active_category,
            authoritative=authoritative,
            authoritative_items=authoritative_items,
            excluded_categories=excluded_categories,
            excluded_outfit_terms=excluded_outfit_terms,
        )
        negative, negative_found, negative_removed = _sanitize_field(
            section,
            payload.negative,
            negative=True,
            active_category=active_category,
            authoritative=authoritative,
            authoritative_items=authoritative_items,
            excluded_categories=excluded_categories,
            excluded_outfit_terms=excluded_outfit_terms,
        )
        detected.extend((*positive_found, *negative_found))
        removed.extend((*positive_removed, *negative_removed))
        if any(
            item["action"] == "section_dropped"
            for item in (*positive_removed, *negative_removed)
        ):
            positive = ""
            negative = ""
        sanitized.append(
            _replace_photo_prompt_section(
                section,
                positive=positive,
                negative=negative,
            )
        )

    residual: list[dict[str, Any]] = []
    for index, section in enumerate(sanitized):
        payload = _photo_content(section)
        if payload is None:
            raise AssertionError("sanitized photo section lost its typed payload")
        if (
            not _section_conflict_sanitization_enabled(section)
            or payload.domain_source in {"user_request", "wardrobe_decision"}
        ):
            continue
        found = _conflicts_in_section(
            section,
            active_category=active_category,
            authoritative=authoritative,
            authoritative_items=authoritative_items,
            excluded_categories=excluded_categories,
            excluded_outfit_terms=excluded_outfit_terms,
        )
        if not found:
            continue
        detected.extend(found)
        for conflict in found:
            removed.append(
                {
                    **conflict,
                    "action": "section_dropped",
                }
            )
        sanitized[index] = _replace_photo_prompt_section(
            section,
            positive="",
            negative="",
        )

    for section in sanitized:
        payload = _photo_content(section)
        if payload is None:
            raise AssertionError("sanitized photo section lost its typed payload")
        if (
            not _section_conflict_sanitization_enabled(section)
            or payload.domain_source in {"user_request", "wardrobe_decision"}
        ):
            continue
        residual.extend(
            _conflicts_in_section(
                section,
                active_category=active_category,
                authoritative=authoritative,
                authoritative_items=authoritative_items,
                excluded_categories=excluded_categories,
                excluded_outfit_terms=excluded_outfit_terms,
            )
        )
    return sanitized, detected, removed, residual


def _scan_residual_conflicts(
    sections: Sequence[PromptSection],
    wardrobe: Any,
) -> list[dict[str, Any]]:
    active_category = str(_value(wardrobe, "category", "") or "").strip().lower()
    authoritative = bool(_value(wardrobe, "lock_outfit", False) and active_category)
    authoritative_items = _specific_outfit_items(_value(wardrobe, "requested_outfit_text", ""))
    excluded_categories = frozenset(
        str(item or "").strip().lower()
        for item in (_value(wardrobe, "excluded_categories", ()) or ())
        if str(item or "").strip()
    )
    excluded_outfit_terms = _excluded_outfit_terms(wardrobe)
    residual: list[dict[str, Any]] = []
    for section in sections:
        payload = _photo_content(section)
        if payload is None:
            raise TypeError("sections must contain PromptSection values with PhotoPromptContent")
        if (
            not _section_conflict_sanitization_enabled(section)
            or payload.domain_source in {"user_request", "wardrobe_decision"}
        ):
            continue
        residual.extend(
            _conflicts_in_section(
                section,
                active_category=active_category,
                authoritative=authoritative,
                authoritative_items=authoritative_items,
                excluded_categories=excluded_categories,
                excluded_outfit_terms=excluded_outfit_terms,
            )
        )
    return residual


def _apply_budget(
    sections: list[PromptSection],
    indexes: list[int],
    budget: int,
    *,
    field: str = "positive",
) -> None:
    remaining = budget
    for index in indexes:
        section = sections[index]
        payload = _photo_content(section)
        if payload is None:
            raise TypeError("sections must contain PromptSection values with PhotoPromptContent")
        if payload.protected or section.title == "global_fixed_prompt":
            continue
        current = getattr(payload, field)
        clipped = _clip(current, remaining, preserve_tail=True) if remaining > 0 else ""
        sections[index] = _replace_photo_prompt_section(section, **{field: clipped})
        remaining -= len(clipped)
        if clipped and remaining > 0:
            remaining -= 1


def _budget_sections(sections: list[PromptSection]) -> list[PromptSection]:
    result = list(sections)
    indexes = lambda *sources: [
        index
        for index, section in enumerate(result)
        if (_photo_content(section) is not None)
        and _photo_content(section).domain_source in sources
    ]
    _apply_budget(result, indexes("wardrobe_decision"), 420)
    _apply_budget(result, indexes("reference_fallback"), 320)
    _apply_budget(result, indexes("scene_context", "visual_memory"), 700)
    _apply_budget(result, indexes("preset"), 140)
    _apply_budget(result, indexes("fixed_prompt"), 600)

    for index in indexes("recent_continuity"):
        section = result[index]
        payload = _photo_content(section)
        if payload is None:
            raise AssertionError("budgeted photo section lost its typed payload")
        if payload.protected:
            continue
        limit = 460 if payload.positive.startswith("Recent-photo continuity:") else 280
        result[index] = _replace_photo_prompt_section(
            section,
            positive=_clip(payload.positive, limit, preserve_tail=True),
        )
    _apply_budget(
        result,
        indexes("edit_contract", "composition", "recent_continuity"),
        680,
    )
    _apply_budget(
        result,
        [
            index
            for index, section in enumerate(result)
            if (_photo_content(section) is not None)
            and _photo_content(section).domain_source
            not in {"user_request", "composition"}
        ],
        230,
        field="negative",
    )
    return result


def _join_field(
    sections: Sequence[PromptSection],
    sources: frozenset[str],
    field: str = "positive",
) -> str:
    return "\n".join(
        value
        for section in sections
        for payload in (_photo_content(section),)
        if payload is not None and payload.domain_source in sources
        for value in (getattr(payload, field).strip(),)
        if value
    )


def _strip_compaction_markers(text: Any) -> str:
    return re.sub(
        r"\s*\.\.\. \[section compacted\] \.\.\.\s*",
        ", ",
        str(text or ""),
    )


def _render_photo_wire(text: str) -> str:
    return render_prompt_sections(
        [
            prompt_section(
                key="photo.rendered_wire",
                title="图片提示词",
                source="photo_prompt_context",
                content=exact_text(text),
            )
        ],
        mode=PromptRenderMode.PHOTO_PROMPT,
    )


def _assemble(sections: Sequence[PromptSection], prompt_format: str) -> str:
    groups = (
        (
            "User image request",
            _join_field(sections, frozenset({"user_request"})),
        ),
        (
            "Reference and wardrobe ruling",
            _join_field(sections, frozenset({"wardrobe_decision", "reference_fallback"})),
        ),
        (
            "Scene, style and final preset",
            _join_field(
                sections,
                frozenset({"scene_context", "visual_memory", "preset", "fixed_prompt"}),
            ),
        ),
        (
            "Composition and continuity",
            _join_field(
                sections,
                frozenset({"edit_contract", "composition", "recent_continuity"}),
            ),
        ),
    )
    positive_blocks = [f"[{label}]\n{_strip_compaction_markers(text)}" for label, text in groups if text]
    user_negative = _join_field(sections, frozenset({"user_request"}), "negative")
    decision_negative = "\n".join(
        payload.negative.strip()
        for section in sections
        for payload in (_photo_content(section),)
        if payload is not None
        and payload.domain_source != "user_request"
        and payload.negative.strip()
    )
    negative = ", ".join(value for value in (decision_negative, user_negative) if value)
    negative = _strip_compaction_markers(negative)
    mode = str(prompt_format or "traditional").strip().lower().replace("-", "_")
    if mode in {"nai", "novelai", "nai4", "nai_4", "nai45", "nai_diffusion", "naidiffusion"}:
        # NAI mode: avoid [] section labels (down-weight syntax) and express negatives via negative weight.
        prompt = "\n\n".join(
            f"{label}:\n{_strip_compaction_markers(text)}" for label, text in groups if text
        )
        wire = f"{prompt}\n\n-1.5::{negative}::".strip() if negative else prompt.strip()
        return _render_photo_wire(wire)
    if mode in {"natural", "natural_language", "description", "prose", "自然语言", "自然语言描述"}:
        prompt = "\n\n".join(positive_blocks)
        wire = f"{prompt}\n\nAvoid {negative}.".strip() if negative else prompt.strip()
        return _render_photo_wire(wire)
    prompt = "Positive prompt:\n" + "\n\n".join(positive_blocks)
    if negative:
        prompt += f"\n\nNegative prompt:\n{negative}"
    return _render_photo_wire(prompt.strip())


_LOCAL_VISUAL_PROMPT_SOURCES = frozenset(
    {
        "user_request",
        "visual_memory",
        "scene_context",
        "preset",
        "fixed_prompt",
        "composition",
    }
)


def _local_visual_section_text(section: PromptSection) -> str:
    """Project a sanitized section into text that an image model may render."""
    payload = _photo_content(section)
    if payload is None:
        raise TypeError("section must carry PhotoPromptContent")
    text = payload.positive.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    text = re.sub(
        r"(?im)^\s*\[(?:User image request|Reference and wardrobe ruling|"
        r"Scene, style and final preset|Composition and continuity)\]\s*$",
        "",
        text,
    )
    if payload.domain_source == "user_request":
        text = re.sub(r"^\s*positive\s+prompt\s*:\s*", "", text, flags=re.I)
        text = re.sub(r"^\s*user\s+request\s*:\s*", "", text, flags=re.I)
    elif payload.domain_source == "scene_context":
        text = re.sub(r"^\s*resolved\s+selfie\s+scene\s+facts\s*:\s*", "", text, flags=re.I)
        text = re.split(
            r"\s*An explicit scene or location in the current request overrides\b",
            text,
            maxsplit=1,
            flags=re.I,
        )[0]
    elif payload.domain_source == "visual_memory":
        text = re.sub(r"^\s*visual\s+continuity\s+reference\s*:\s*", "", text, flags=re.I)
    elif payload.domain_source == "preset":
        text = re.sub(r"^\s*scene\s+preset\s*:\s*", "", text, flags=re.I)
    elif payload.domain_source == "fixed_prompt":
        text = re.sub(r"^\s*additional\s+(?:fixed\s+prompt|outfit\s+preference)\s*:\s*", "", text, flags=re.I)
    elif payload.domain_source == "composition":
        lower_name = section.title.strip().lower()
        if lower_name == "relationship_role_reference":
            text = "two distinct people in one coherent scene" if "shared frame" in text.lower() else ""
        elif lower_name == "composition":
            if "multi-person" in text.lower():
                text = "multi-person portrait, distinct people, one coherent scene"
            elif "back-view" in text.lower():
                text = "single character, back view, coherent outfit, natural environmental portrait"
            elif "mirror" in text.lower():
                text = "single character mirror portrait, coherent outfit, visible face"
            else:
                text = "single character, coherent outfit, one continuous scene, visible face, upper-body or three-quarter portrait"
        elif lower_name == "subject_count":
            text = "multi-person portrait, distinct referenced people" if "multi-person" in text.lower() else "single character, one person"
        else:
            text = ""
    text = text.replace(_COMPACTED_MARKER, ", ")
    text = re.sub(r"\s*\n+\s*", ", ", text)
    text = re.sub(r"\s*;\s*", ", ", text)
    text = re.sub(r"(?:\s*,\s*){2,}", ", ", text)
    return text.strip(" \t\r\n,.;；。")


def compile_local_photo_prompt(
    sections: Sequence[PromptSection],
    prompt_format: str,
) -> str:
    """Compile renderable positive content for single-text local image workflows.

    Traditional prompt envelopes contain orchestration metadata intended for an
    LLM. ComfyUI/SDGen often feed their only text input directly to CLIP/T5, so
    those labels, decisions and negative blocks must not share that input.
    """
    normalized_sections: list[PromptSection] = []
    for value in sections:
        section = _validated_photo_prompt_section(value)
        if section is None:
            raise TypeError("sections must contain compatible photo prompt sections")
        normalized_sections.append(section)
    mode = str(prompt_format or "traditional").strip().lower().replace("-", "_")
    if mode != "traditional":
        return _assemble(normalized_sections, mode)
    values: list[str] = []
    seen: set[str] = set()
    for section in normalized_sections:
        payload = _photo_content(section)
        if payload is None:
            raise AssertionError("normalized photo section lost its typed payload")
        if payload.domain_source not in _LOCAL_VISUAL_PROMPT_SOURCES:
            continue
        value = _local_visual_section_text(section)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            values.append(value)
    return _render_photo_wire(", ".join(values).strip())


def _reference_roles(reference: Any, wardrobe: Any) -> tuple[str, ...]:
    original = _value(reference, "reference_roles", ()) or ()
    effective = _value(wardrobe, "effective_reference_roles", ()) or ()
    return tuple(
        dict.fromkeys(
            str(item or "").strip().lower()
            for item in (*original, *effective)
            if str(item or "").strip()
        )
    )


def _sanitize_reference(reference: Any, wardrobe: Any, workflow_kind: str) -> tuple[Any, dict[str, Any] | None]:
    if reference is None:
        return None, None
    workflow = str(workflow_kind or "").strip().lower()
    if workflow in _EDIT_WORKFLOWS or workflow not in _SELFIE_WORKFLOWS:
        return reference, None
    roles = _reference_roles(reference, wardrobe)
    ignored_roles = frozenset(
        str(item or "").strip().lower()
        for item in (_value(reference, "ignored_reference_roles", ()) or ())
        if str(item or "").strip()
    )
    if "outfit" not in roles and "outfit" not in ignored_roles:
        return reference, None
    if (
        str(_value(reference, "kind", "") or "").strip().lower() == "recent_sent_photo"
        and "outfit" in ignored_roles
    ):
        return reference, None
    active_category = str(_value(wardrobe, "category", "") or "").strip().lower()
    authoritative = bool(_value(wardrobe, "lock_outfit", False) and active_category)
    excluded = frozenset(
        str(item or "").strip().lower()
        for item in (_value(wardrobe, "excluded_categories", ()) or ())
        if str(item or "").strip()
    )
    reference_category = str(_value(reference, "outfit_category", "") or "").strip().lower()
    reason = ""
    category = reference_category or "unknown"
    if reference_category and reference_category in excluded:
        reason = "reference_outfit_excluded"
    elif authoritative and reference_category and not _compatible(reference_category, active_category):
        reason = "reference_outfit_conflict"
    elif authoritative and not reference_category and active_category not in {"reference_outfit"}:
        reason = "reference_outfit_unknown"
    if not reason:
        return reference, None
    raw = str(_value(reference, "id", "") or _value(reference, "source", "") or "reference")
    return None, {
        "source": "reference",
        "section": raw,
        "rule": reason,
        "category": category,
        "action": "reference_removed",
        "preview": _preview(raw),
        "sha256": hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest(),
        "effective_reference_roles": [],
    }


def _remove_reference_dependent_context(
    sections: Sequence[PromptSection],
) -> tuple[list[PromptSection], list[dict[str, Any]], list[dict[str, Any]]]:
    sanitized: list[PromptSection] = []
    detected: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    reference_pattern = re.compile(
        r"\b(?:this|the|selected|provided|incompatible)\s+(?:image\s+)?reference\b"
        r"|\breference\s+(?:image|controls|responsibility)\b",
        flags=re.I,
    )
    for section in sections:
        payload = _photo_content(section)
        if payload is None:
            raise TypeError("sections must contain PromptSection values with PhotoPromptContent")
        if payload.domain_source == "user_request":
            sanitized.append(section)
            continue
        if payload.domain_source == "recent_continuity" and (
            payload.positive or payload.negative
        ):
            for text in (payload.positive, payload.negative):
                if not text:
                    continue
                detected.append(
                    _audit(
                        section,
                        rule="reference_context_removed",
                        category="reference",
                        action="detected",
                        text=text,
                    )
                )
                removed.append(
                    _audit(
                        section,
                        rule="reference_context_removed",
                        category="reference",
                        action="section_dropped",
                        text=text,
                    )
                )
            sanitized.append(
                _replace_photo_prompt_section(
                    section,
                    positive="",
                    negative="",
                )
            )
            continue

        fields: dict[str, str] = {}
        for field in ("positive", "negative"):
            text = getattr(payload, field)
            clauses, separator = _split_clauses(text)
            kept: list[str] = []
            for clause in clauses:
                if not reference_pattern.search(clause):
                    kept.append(clause)
                    continue
                detected.append(
                    _audit(
                        section,
                        rule="reference_context_removed",
                        category="reference",
                        action="detected",
                        text=clause,
                    )
                )
                removed.append(
                    _audit(
                        section,
                        rule="reference_context_removed",
                        category="reference",
                        action="clause_removed",
                        text=clause,
                    )
                )
            fields[field] = separator.join(kept)
        sanitized.append(_replace_photo_prompt_section(section, **fields))
    return sanitized, detected, removed


def resolve_photo_prompt_context(
    *,
    wardrobe: Any,
    sections: Sequence[PromptSection],
    prompt_format: str,
    workflow_kind: str,
    reference: Any = None,
) -> ResolvedPhotoPromptContext:
    clean_reference, reference_removed = _sanitize_reference(reference, wardrobe, workflow_kind)
    prepared: list[PromptSection] = []
    for value in sections:
        section = _validated_photo_prompt_section(value)
        if section is None:
            raise TypeError("sections must contain compatible photo prompt sections")
        prepared.append(section)
    detected: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    if reference_removed:
        prepared, reference_detected, reference_removals = _remove_reference_dependent_context(prepared)
        detected.extend(reference_detected)
        removed.extend(reference_removals)
    sanitized, section_detected, section_removed, residual = _sanitize_sections(tuple(prepared), wardrobe)
    detected.extend(section_detected)
    removed.extend(section_removed)
    complete_prompt = _assemble(sanitized, prompt_format)
    budgeted = _budget_sections(sanitized)
    residual.extend(_scan_residual_conflicts(budgeted, wardrobe))
    return ResolvedPhotoPromptContext(
        final_prompt=_assemble(budgeted, prompt_format),
        complete_prompt=complete_prompt,
        prompt_sections=tuple(budgeted),
        reference=clean_reference,
        detected_conflicts=tuple(detected),
        removed_conflicts=tuple(removed),
        residual_conflicts=tuple(residual),
        reference_removed=reference_removed,
    )
