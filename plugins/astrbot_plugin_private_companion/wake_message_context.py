# -*- coding: utf-8 -*-
"""Preserve addressed chat text without changing AstrBot command routing."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .conversation_injection_plan import replace_conversation_turn_prompt


WAKE_MESSAGE_CONTEXT_ATTR = "_private_companion_wake_message_context"


def _event_config(owner: Any, event: Any) -> Mapping:
    getter = getattr(getattr(owner, "context", None), "get_config", None)
    if not callable(getter):
        return {}
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    try:
        config = getter(umo=umo)
    except TypeError:
        try:
            config = getter(umo)
        except Exception:
            return {}
    except Exception:
        return {}
    return config if isinstance(config, Mapping) else {}


def capture_wake_message_context(owner: Any, event: Any) -> None:
    """Publish a turn-local source only when the configured prefix explains the loss."""
    if not bool(getattr(event, "is_at_or_wake_command", False)):
        return
    if bool(getattr(event, "private_companion_proactive_framework", False)):
        return
    original = getattr(getattr(event, "message_obj", None), "message_str", None)
    routed = getattr(event, "message_str", None)
    if not isinstance(original, str) or not isinstance(routed, str):
        return
    original, routed = original.strip(), routed.strip()
    if not original or original == routed:
        return
    if owner._message_debounce_command_text(event, routed):
        return
    inbound_checker = getattr(owner, "_event_is_inbound_chat_message", None)
    if callable(inbound_checker) and not inbound_checker(event):
        return
    prefixes = _event_config(owner, event).get("wake_prefix", [])
    if not isinstance(prefixes, (list, tuple)):
        return
    for prefix in prefixes:
        if not isinstance(prefix, str) or not original.startswith(prefix):
            continue
        if (
            any(char.isalnum() for char in prefix)
            and original[len(prefix):].strip() == routed
        ):
            setattr(event, WAKE_MESSAGE_CONTEXT_ATTR, {
                "original_text": original,
                "routed_text": routed,
                "wake_prefix": prefix,
            })
        return


def restore_wake_message_request(owner: Any, event: Any, req: Any) -> bool:
    """Restore only the current prompt, keeping history, tools and attachments intact."""
    capture_wake_message_context(owner, event)
    snapshot = getattr(event, WAKE_MESSAGE_CONTEXT_ATTR, None)
    if not isinstance(snapshot, dict) or req is None:
        return False
    original, routed = snapshot.get("original_text"), snapshot.get("routed_text")
    current = getattr(event, "message_str", None)
    source = getattr(getattr(event, "message_obj", None), "message_str", None)
    if (
        not isinstance(original, str) or not isinstance(routed, str)
        or not isinstance(current, str) or current.strip() != routed
        or not isinstance(source, str) or source.strip() != original
        or owner._message_debounce_command_text(event, current)
        or bool(getattr(event, "private_companion_proactive_framework", False))
    ):
        return False
    prompt = getattr(req, "prompt", None)
    if not isinstance(prompt, str):
        return False
    if prompt.strip() == routed:
        return replace_conversation_turn_prompt(req, original)
    settings = _event_config(owner, event).get("provider_settings", {})
    prefix = settings.get("prompt_prefix") if isinstance(settings, Mapping) else None
    if isinstance(prefix, str) and prefix:
        def decorate(text: str) -> str:
            return prefix.replace("{{prompt}}", text) if "{{prompt}}" in prefix else prefix + text

        if prompt.strip() == decorate(routed).strip():
            return replace_conversation_turn_prompt(req, decorate(original))
    return False
