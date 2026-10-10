# -*- coding: utf-8 -*-
"""Compatibility facade for the relocated external bridge resolver.

The implementation lives under ``companion.integrations``.  Keeping this
module preserves imports used by older integrations and installations.
"""
from __future__ import annotations

try:
    from .companion.integrations import external_bridge_resolver as _implementation
except ImportError:  # pragma: no cover - direct import from the plugin directory
    from companion.integrations import external_bridge_resolver as _implementation  # type: ignore


def __getattr__(name: str):
    return getattr(_implementation, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_implementation)))
