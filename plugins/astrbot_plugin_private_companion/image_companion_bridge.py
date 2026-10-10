# -*- coding: utf-8 -*-
"""Compatibility facade for the relocated Image companion bridge."""
from __future__ import annotations

try:
    from .companion.integrations import image_companion_bridge as _implementation
except ImportError:  # pragma: no cover - direct import from the plugin directory
    from companion.integrations import image_companion_bridge as _implementation  # type: ignore

ImageCompanionBridgeMixin = _implementation.ImageCompanionBridgeMixin


def __getattr__(name: str):
    return getattr(_implementation, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_implementation)))
