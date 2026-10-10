"""Compatibility import for :mod:`domains.affect.emotion_event_ledger`."""

try:
    from .domains.affect.emotion_event_ledger import *  # noqa: F403
except ImportError:  # pragma: no cover - direct-module compatibility
    from domains.affect.emotion_event_ledger import *  # type: ignore # noqa: F403
