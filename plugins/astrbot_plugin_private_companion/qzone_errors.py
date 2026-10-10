# -*- coding: utf-8 -*-
"""Shared QQ Zone failure types."""
from __future__ import annotations

__all__ = ("QzoneIntegrationError",)

class QzoneIntegrationError(RuntimeError):
    """User-facing Qzone error with a coarse failure stage."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        retryable: bool = False,
        delivery_unknown: bool = False,
    ):
        self.stage = stage
        self.retryable = bool(retryable)
        self.delivery_unknown = bool(delivery_unknown)
        super().__init__(f"{stage}：{message}")
