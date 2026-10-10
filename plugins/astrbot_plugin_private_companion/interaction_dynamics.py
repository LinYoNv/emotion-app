"""Compatibility import for :mod:`domains.affect.interaction_dynamics`."""

try:
    from .domains.affect.interaction_dynamics import *  # noqa: F403
except ImportError:  # pragma: no cover - direct-module compatibility
    from domains.affect.interaction_dynamics import *  # type: ignore # noqa: F403
