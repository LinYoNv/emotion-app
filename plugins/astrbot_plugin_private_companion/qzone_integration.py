# -*- coding: utf-8 -*-
"""Compatibility facade for the decomposed QQ Zone integration.

The public plugin surface remains :class:`QzoneMixin`; concrete responsibilities
live in dedicated runtime, feed, comment, publishing, and schedule mixins.
"""
from __future__ import annotations

# Kept as a compatibility export for integrations that patch random choices in
# the automatic publish workflow. All modules share the same stdlib module.
import random

from .qzone_comments import QzoneCommentMixin
from .qzone_feed import QzoneFeedMixin
from .qzone_errors import QzoneIntegrationError
from .qzone_media import QzoneMediaMixin
from .qzone_publish import QzonePublishMixin
from .qzone_runtime import QzoneRuntimeMixin
from .qzone_schedule import (
    QZONE_INTRA_DAY_GAP_FLOOR_MINUTES,
    QZONE_LENGTH_HARD_LIMIT,
    QZONE_LENGTH_PROFILES,
    QZONE_NIGHT_RANGES,
    QZONE_PLAN_ITEM_MAX_ATTEMPTS,
    QZONE_WINDOW_TEMPLATE_DOUBLE,
    QZONE_WINDOW_TEMPLATE_DOUBLE_NIGHT,
    QzoneScheduleMixin,
)

__all__ = [
    "QzoneIntegrationError",
    "QzoneMediaMixin",
    "QzoneMixin",
    "random",
    "QZONE_INTRA_DAY_GAP_FLOOR_MINUTES",
    "QZONE_LENGTH_HARD_LIMIT",
    "QZONE_LENGTH_PROFILES",
    "QZONE_NIGHT_RANGES",
    "QZONE_PLAN_ITEM_MAX_ATTEMPTS",
    "QZONE_WINDOW_TEMPLATE_DOUBLE",
    "QZONE_WINDOW_TEMPLATE_DOUBLE_NIGHT",
]


class QzoneMixin(
    QzoneCommentMixin,
    QzoneScheduleMixin,
    QzonePublishMixin,
    QzoneFeedMixin,
    QzoneRuntimeMixin,
    QzoneMediaMixin,
):
    """Stable QQ Zone mixin assembled from focused implementation modules."""
