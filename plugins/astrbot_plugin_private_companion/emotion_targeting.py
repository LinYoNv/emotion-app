"""Compatibility import for :mod:`domains.affect.emotion_targeting`."""

try:
    from .domains.affect.emotion_targeting import *  # noqa: F403
except ImportError:  # pragma: no cover - direct-module compatibility
    from domains.affect.emotion_targeting import *  # type: ignore # noqa: F403
