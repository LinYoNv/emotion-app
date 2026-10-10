from __future__ import annotations

import copy
from typing import Any


def _role(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("role") or "").strip().lower()


def _is_tool_result(item: Any) -> bool:
    return _role(item) == "tool"


def _tool_call_ids(tool_calls: Any) -> list[str] | None:
    """Return strict OpenAI-style tool-call IDs, or None for malformed calls."""
    if not isinstance(tool_calls, list) or not tool_calls:
        return None
    identifiers: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            return None
        call_id = str(call.get("id") or "").strip()
        if not call_id or call_id in identifiers:
            return None
        identifiers.append(call_id)
    return identifiers


def _tool_result_ids(items: list[Any]) -> list[str] | None:
    identifiers: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        call_id = str(item.get("tool_call_id") or "").strip()
        if not call_id:
            return None
        identifiers.append(call_id)
    return identifiers


def sanitize_openai_tool_history(contexts: Any) -> tuple[Any, dict[str, int]]:
    """Remove only malformed OpenAI tool-call groups from a generic history list.

    The function does not inspect content or mutate retained entries. A no-op
    returns the original list object so ordinary history remains untouched.
    """
    stats = {
        "changed": 0,
        "removed_groups": 0,
        "removed_assistants": 0,
        "removed_tool_results": 0,
        "removed_orphans": 0,
    }
    if not isinstance(contexts, list) or not contexts:
        return contexts, stats

    kept: list[Any] = []
    index = 0
    while index < len(contexts):
        item = contexts[index]
        role = _role(item)
        if role == "tool":
            stats["changed"] = 1
            stats["removed_tool_results"] += 1
            stats["removed_orphans"] += 1
            index += 1
            continue

        raw_calls = item.get("tool_calls") if isinstance(item, dict) and role == "assistant" else None
        has_effective_calls = raw_calls not in (None, [])
        if not has_effective_calls:
            kept.append(item)
            index += 1
            continue

        next_index = index + 1
        result_items: list[Any] = []
        while next_index < len(contexts) and _is_tool_result(contexts[next_index]):
            result_items.append(contexts[next_index])
            next_index += 1

        call_ids = _tool_call_ids(raw_calls)
        result_ids = _tool_result_ids(result_items)
        valid = bool(
            call_ids
            and result_ids is not None
            and len(result_ids) == len(call_ids)
            and len(set(result_ids)) == len(result_ids)
            and set(result_ids) == set(call_ids)
        )
        if valid:
            kept.append(item)
            kept.extend(result_items)
        else:
            stats["changed"] = 1
            stats["removed_groups"] += 1
            stats["removed_assistants"] += 1
            stats["removed_tool_results"] += len(result_items)
        index = next_index

    if not stats["changed"]:
        return contexts, stats
    return kept, stats


def sanitize_history_image_blocks(contexts: Any) -> tuple[Any, dict[str, int]]:
    """Replace historical image blocks without touching current request images."""
    stats = {
        "changed": 0,
        "messages_changed": 0,
        "image_blocks_replaced": 0,
    }
    if not isinstance(contexts, list) or not contexts:
        return contexts, stats

    cleaned: list[Any] = []
    for item in contexts:
        if isinstance(item, dict):
            message = copy.deepcopy(item)
        else:
            dump = getattr(item, "model_dump", None)
            if not callable(dump):
                cleaned.append(item)
                continue
            try:
                message = dump()
            except Exception:
                cleaned.append(item)
                continue
            if not isinstance(message, dict):
                cleaned.append(item)
                continue

        content = message.get("content")
        if not isinstance(content, list):
            cleaned.append(item)
            continue

        retained: list[Any] = []
        removed = 0
        for part in content:
            part_data = part
            if not isinstance(part_data, dict):
                dump = getattr(part_data, "model_dump", None)
                if callable(dump):
                    try:
                        part_data = dump()
                    except Exception:
                        part_data = part
            part_type = str(part_data.get("type") or "").strip().lower() if isinstance(part_data, dict) else ""
            if part_type in {"image_url", "image"}:
                removed += 1
                continue
            retained.append(copy.deepcopy(part_data))

        if removed <= 0:
            cleaned.append(item)
            continue
        retained.append({"type": "text", "text": "[历史图片]"})
        message["content"] = retained
        cleaned.append(message)
        stats["changed"] = 1
        stats["messages_changed"] += 1
        stats["image_blocks_replaced"] += removed

    if not stats["changed"]:
        return contexts, stats
    return cleaned, stats
