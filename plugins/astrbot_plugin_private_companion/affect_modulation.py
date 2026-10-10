"""Compatibility import for :mod:`domains.affect.affect_modulation`."""

try:
    from .domains.affect.affect_modulation import *  # noqa: F403
except ImportError:  # pragma: no cover - direct-module compatibility
    from domains.affect.affect_modulation import *  # type: ignore # noqa: F403
