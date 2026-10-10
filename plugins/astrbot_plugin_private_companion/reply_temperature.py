"""Compatibility import for :mod:`domains.affect.reply_temperature`."""

try:
    from .domains.affect.reply_temperature import *  # noqa: F403
except ImportError:  # pragma: no cover - direct-module compatibility
    from domains.affect.reply_temperature import *  # type: ignore # noqa: F403
