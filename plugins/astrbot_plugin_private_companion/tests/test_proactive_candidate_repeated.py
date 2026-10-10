# -*- coding: utf-8 -*-
"""Regression tests: proactive candidate duplicate detection.

外部分享类候选的 motive 是统一模板文本，原先参与 _proactive_topic_signature
会把 topic 不同的内容误判为重复。修复后主题相似判定只基于 topic。
"""
from astrbot_plugin_private_companion.daily_state import DailyStateMixin
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin

import time


class _CandidateRepeatHarness(ProactiveEngineMixin, DailyStateMixin):
    pass


_MOTIVE_TEMPLATE = "刚读到的新闻和自己的能力、兴趣或最近状态有一点关系,想按人格私下找用户说说"


def _harness() -> _CandidateRepeatHarness:
    harness = _CandidateRepeatHarness()
    harness.data = {"proactive_candidate_pool": [], "users": {}}
    return harness


def test_different_news_topics_with_shared_motive_template_are_not_repeated():
    harness = _harness()
    # _proactive_candidate_repeated 内部用 _now_ts()（真实时间）过滤 8h 窗口，
    # 所以候选时间戳必须贴近当前时间，不能用固定常数。
    now = time.time()
    user = {"user_id": "u1", "recent_proactive_topics": []}
    # 候选池内已有 8h 内 sent 的 news 候选：Koei TGS（topic 与待测候选不同，
    # 但 motive 是同一条模板文本——修复前正是这里被误判为相似）。
    harness.data["proactive_candidate_pool"].append(
        {
            "user_id": "u1",
            "status": "sent",
            "kind": "content_share",
            "reason": "news_share",
            "source": "news",
            "created_ts": now - 3600,
            "signature": harness._proactive_topic_signature(
                "Koei Tecmo announces TGS 2026 lineup",
                _MOTIVE_TEMPLATE,
            ),
        }
    )
    candidate = {
        "kind": "content_share",
        "reason": "news_share",
        "source": "news",
        "topic": "中元已近，黑暗降临。卡牌战斗游戏《恶魔召唤》正式加入机核发行",
        "motive": _MOTIVE_TEMPLATE,
    }

    assert harness._proactive_candidate_repeated(user, candidate) is False


def test_same_news_topic_repeated_is_still_blocked():
    harness = _harness()
    now = time.time()
    user = {"user_id": "u1", "recent_proactive_topics": []}
    topic = "Koei Tecmo announces TGS 2026 lineup"
    harness.data["proactive_candidate_pool"].append(
        {
            "user_id": "u1",
            "status": "sent",
            "kind": "content_share",
            "reason": "news_share",
            "source": "news",
            "created_ts": now - 3600,
            "signature": harness._proactive_topic_signature(topic),
        }
    )
    candidate = {
        "kind": "content_share",
        "reason": "news_share",
        "source": "news",
        "topic": topic,
        "motive": _MOTIVE_TEMPLATE,
    }

    assert harness._proactive_candidate_repeated(user, candidate) is True


def test_same_topic_different_motive_wording_is_still_blocked():
    # 同一新闻 topic 换一种 motive 措辞，仍应判重复（topic 是唯一判定维度）。
    harness = _harness()
    now = time.time()
    user = {"user_id": "u1", "recent_proactive_topics": []}
    topic = "Blue Box Season 2 Reveals Main Visual"
    harness.data["proactive_candidate_pool"].append(
        {
            "user_id": "u1",
            "status": "sent",
            "kind": "content_share",
            "reason": "news_share",
            "source": "news",
            "created_ts": now - 3600,
            "signature": harness._proactive_topic_signature(topic),
        }
    )
    candidate = {
        "kind": "content_share",
        "reason": "news_share",
        "source": "news",
        "topic": topic,
        "motive": "换了一种表达想法的动机文本",
    }

    assert harness._proactive_candidate_repeated(user, candidate) is True
