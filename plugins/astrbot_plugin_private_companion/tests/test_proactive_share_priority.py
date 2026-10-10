# -*- coding: utf-8 -*-
"""Regression tests: configurable share priority and preempt queue expiry.

外部分享类（content_share 路线）候选原先在 _proactive_impulse_orchestration_priority
中按 source 硬编码（news 等落默认 48），活跃时段排不过 habit/meal 等而难以晋升；
排队候选的存活时长也是硬编码 now+2h。两个参数化：
- proactive_share_priority：Share 类 reason 的编排优先级（默认 48 与硬编码一致）
- proactive_preempt_queue_expire_hours：排队候选最长存活小时数（默认 2 与原值一致）
"""
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin


class _PriorityHarness(ProactiveEngineMixin):
    pass


def test_share_priority_default_matches_hardcoded():
    harness = _PriorityHarness()

    assert harness._proactive_impulse_orchestration_priority(
        {"source": "news", "reason": "news_share"}
    ) == 48


def test_share_priority_config_overrides_news():
    harness = _PriorityHarness()
    harness.proactive_share_priority = 78

    assert harness._proactive_impulse_orchestration_priority(
        {"source": "news", "reason": "news_share"}
    ) == 78


def test_share_priority_applies_to_other_share_reasons():
    harness = _PriorityHarness()
    harness.proactive_share_priority = 72

    assert harness._proactive_impulse_orchestration_priority(
        {"source": "bilibili", "reason": "bili_video_share"}
    ) == 72
    assert harness._proactive_impulse_orchestration_priority(
        {"source": "web_exploration", "reason": "web_exploration_share"}
    ) == 72
    assert harness._proactive_impulse_orchestration_priority(
        {"source": "creative_writing", "reason": "creative_share"}
    ) == 72


def test_share_priority_does_not_affect_other_routes():
    harness = _PriorityHarness()
    harness.proactive_share_priority = 78

    # 非 Share 类路线不受影响：habit 仍 64、meal_care 仍 78。
    assert harness._proactive_impulse_orchestration_priority(
        {"source": "habit", "reason": "habit_awareness"}
    ) == 64
    assert harness._proactive_impulse_orchestration_priority(
        {"source": "meal_care", "reason": "meal_care"}
    ) == 78
