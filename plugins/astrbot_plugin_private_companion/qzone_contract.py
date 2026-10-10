# -*- coding: utf-8 -*-
"""Compatibility facade for the relocated Qzone contract."""
from __future__ import annotations

try:
    from .companion.contracts import qzone_contract as _implementation
except ImportError:  # pragma: no cover - direct import from the plugin directory
    from companion.contracts import qzone_contract as _implementation  # type: ignore


def __getattr__(name: str):
    return getattr(_implementation, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_implementation)))
