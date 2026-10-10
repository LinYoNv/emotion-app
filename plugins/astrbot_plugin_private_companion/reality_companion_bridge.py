# -*- coding: utf-8 -*-
"""Compatibility facade for the relocated Reality companion bridge."""
from __future__ import annotations

try:
    from .companion.integrations import reality_companion_bridge as _implementation
except ImportError:  # pragma: no cover - direct import from the plugin directory
    from companion.integrations import reality_companion_bridge as _implementation  # type: ignore

RealityCompanionBridgeMixin = _implementation.RealityCompanionBridgeMixin


def __getattr__(name: str):
    return getattr(_implementation, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_implementation)))
