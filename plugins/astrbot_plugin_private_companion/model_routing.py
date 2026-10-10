# -*- coding: utf-8 -*-
"""Local model replacement rules shared by plugin and conversation routes."""
from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Pattern, Sequence


MATCH_MODES = {"contains", "exact", "regex"}
KEYWORD_LOGICS = {"any", "all"}
MODEL_REPLACEMENT_SCOPES = {"plugin", "conversation", "all"}
CURRENT_MODEL_REPLACEMENT_SOURCES: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "private_companion_model_replacement_sources",
    default=(),
)

DEFAULT_SENSITIVE_REPLACEMENT_KEYWORDS = (
    "很抱歉，我无法",
    "很抱歉,我无法",
    "我有我自己的底线",
    "我们可以聊聊别的",
    "我无法满足",
    "露骨性行为",
    "没办法提交这个请求",
    "这个请求没办法提交",
    "The prompt could not be submitted",
    "prompt could not be submitted",
    "The request could not be submitted",
    "request could not be submitted",
)


@dataclass(frozen=True, slots=True)
class ModelReplacementRule:
    name: str
    provider_id: str
    model: str
    keywords: tuple[str, ...]
    match_mode: str
    keyword_logic: str
    case_sensitive: bool
    priority: int
    order: int
    patterns: tuple[Pattern[str], ...] = field(default=(), repr=False, compare=False)

    def match(self, message: str) -> str | None:
        if not message:
            return None
        if self.match_mode == "regex":
            matches = [pattern.search(message) is not None for pattern in self.patterns]
        else:
            target = message if self.case_sensitive else message.casefold()
            candidates = self.keywords if self.case_sensitive else tuple(item.casefold() for item in self.keywords)
            if self.match_mode == "exact":
                target = target.strip()
                matches = [target == item.strip() for item in candidates]
            else:
                matches = [item in target for item in candidates]
        if not matches:
            return None
        if self.match_mode != "exact" and self.keyword_logic == "all":
            return ", ".join(self.keywords) if all(matches) else None
        for keyword, matched in zip(self.keywords, matches):
            if matched:
                return keyword
        return None


@dataclass(frozen=True, slots=True)
class ModelReplacementMatch:
    rule: ModelReplacementRule
    matched_keyword: str
    source: str


def normalize_scope(value: Any, default: str = "plugin") -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "插件": "plugin",
        "插件调用": "plugin",
        "plugin": "plugin",
        "conversation": "conversation",
        "对话": "conversation",
        "对话模型": "conversation",
        "全部": "all",
        "all": "all",
    }
    return aliases.get(text, default if default in MODEL_REPLACEMENT_SCOPES else "plugin")


def scope_allows(scope: Any, target: str) -> bool:
    normalized = normalize_scope(scope)
    target = "conversation" if target == "conversation" else "plugin"
    return normalized in {target, "all"}


def normalize_keywords(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = re.split(r"[,，;；\n]+", value)
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def normalize_rule_configs(value: Any) -> list[dict[str, Any]]:
    """Keep editable rule data JSON-safe while preserving disabled rules."""
    if isinstance(value, str):
        try:
            import json

            value = json.loads(value or "[]")
        except Exception:
            return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in value[:80]:
        if not isinstance(raw, dict):
            continue
        keywords = list(normalize_keywords(raw.get("keywords", [])))[:40]
        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        match_mode = str(raw.get("match_mode") or "contains").strip().lower()
        if match_mode not in MATCH_MODES:
            match_mode = "contains"
        keyword_logic = str(raw.get("keyword_logic") or "any").strip().lower()
        if keyword_logic not in KEYWORD_LOGICS:
            keyword_logic = "any"
        normalized.append(
            {
                "name": str(raw.get("name") or "").strip()[:120],
                "enabled": bool(raw.get("enabled", True)),
                "priority": max(-100000, min(100000, priority)),
                "keywords": keywords,
                "match_mode": match_mode,
                "keyword_logic": keyword_logic,
                "case_sensitive": bool(raw.get("case_sensitive", False)),
                "provider_id": str(raw.get("provider_id") or "").strip()[:160],
                "model": str(raw.get("model") or "").strip()[:160],
            }
        )
    return normalized


def build_rules(raw_rules: Any) -> tuple[list[ModelReplacementRule], list[str]]:
    warnings: list[str] = []
    if raw_rules is None:
        return [], warnings
    normalized_configs = normalize_rule_configs(raw_rules)
    if raw_rules not in (None, "") and not normalized_configs and not isinstance(raw_rules, (list, tuple, dict, str)):
        return [], ["model_replacement_rules 必须是列表"]
    rules: list[ModelReplacementRule] = []
    for index, item in enumerate(normalized_configs):
        label = f"第 {index + 1} 条规则"
        if not isinstance(item, dict) or not bool(item.get("enabled", True)):
            continue
        name = str(item.get("name") or label).strip() or label
        provider_id = str(item.get("provider_id") or "").strip()
        keywords = normalize_keywords(item.get("keywords", []))
        if not provider_id:
            warnings.append(f"{name}未配置 Provider，已忽略")
            continue
        if not keywords:
            warnings.append(f"{name}没有有效关键词，已忽略")
            continue
        match_mode = str(item.get("match_mode") or "contains").strip().lower()
        if match_mode not in MATCH_MODES:
            match_mode = "contains"
        keyword_logic = str(item.get("keyword_logic") or "any").strip().lower()
        if keyword_logic not in KEYWORD_LOGICS:
            keyword_logic = "any"
        try:
            priority = int(item.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        case_sensitive = bool(item.get("case_sensitive", False))
        patterns: tuple[Pattern[str], ...] = ()
        if match_mode == "regex":
            try:
                flags = 0 if case_sensitive else re.IGNORECASE
                patterns = tuple(re.compile(keyword, flags) for keyword in keywords)
            except re.error as exc:
                warnings.append(f"{name}包含无效正则表达式，已忽略：{exc}")
                continue
        rules.append(
            ModelReplacementRule(
                name=name,
                provider_id=provider_id,
                model=str(item.get("model") or "").strip(),
                keywords=keywords,
                match_mode=match_mode,
                keyword_logic=keyword_logic,
                case_sensitive=case_sensitive,
                priority=priority,
                order=index,
                patterns=patterns,
            )
        )
    rules.sort(key=lambda item: (-item.priority, item.order))
    return rules, warnings


def find_route(rules: Sequence[ModelReplacementRule], sources: Sequence[tuple[str, str]]) -> ModelReplacementMatch | None:
    normalized = [
        (str(source or "unknown"), text)
        for source, text in sources
        if isinstance(text, str) and text.strip()
    ]
    for rule in rules:
        for source, text in normalized:
            matched = rule.match(text)
            if matched is not None:
                return ModelReplacementMatch(rule=rule, matched_keyword=matched, source=source)
    return None


def contains_sensitive_refusal(text: Any, keywords: Any = None) -> str:
    cleaned = re.sub(r"\s+", "", str(text or "")).casefold()
    if not cleaned:
        return ""
    # Custom terms extend the built-in high-confidence provider refusal terms.
    # Keeping the built-ins active prevents an older saved custom list from
    # disabling detection for newly observed provider error wording.
    candidates = tuple(dict.fromkeys((*DEFAULT_SENSITIVE_REPLACEMENT_KEYWORDS, *normalize_keywords(keywords))))
    for keyword in candidates:
        compact = re.sub(r"\s+", "", keyword).casefold()
        if compact and compact in cleaned:
            return keyword
    return ""
