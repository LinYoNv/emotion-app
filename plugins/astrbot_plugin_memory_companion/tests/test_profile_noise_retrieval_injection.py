from __future__ import annotations

import asyncio
import unittest

from core.injection import InjectionComposer
from core.models import (
    EntityRef,
    MemoryRecord,
    SearchResult,
    SessionContext,
)
from core.retrieval import RetrievalEngine
from core.visibility import VisibilityPolicy


def rule_profile(
    memory_id: str,
    value: str = "宝贝",
    *,
    state: str = "active",
    score: float = 0.97,
) -> MemoryRecord:
    return MemoryRecord(
        id=memory_id,
        memory_type="user_profile",
        subject=EntityRef(kind="user", id="u1"),
        object=EntityRef.bot_self("b1"),
        scope="private",
        session_id="qq:FriendMessage:u1",
        platform="qq",
        visibility="private_pair",
        sayability="direct",
        reality_level="real_user_fact",
        lifecycle="stable_memory",
        content=f"希望被称为 {value}",
        evidence=f"以后叫我{value}",
        confidence=score,
        importance=0.8,
        metadata={
            "extractor": "rule_v2",
            "profile_dimension": "preferred_address",
            "profile_value": value,
            "normalized_value": value,
            "extraction_quality": "explicit",
            "extraction_quality_score": score,
            "evidence_strength": "direct_statement",
            "profile_state": state,
            "quality_gate_passed": score >= 0.75,
        },
    )


def compatible_profile(memory_id: str, source: str) -> MemoryRecord:
    metadata = {
        "profile_dimension": "preferred_address",
        "profile_value": "人工称呼",
        "normalized_value": "人工称呼",
        "producer_kind": source,
    }
    if source == "tool_confirmed":
        metadata["tool"] = "memory_companion_remember"
    return MemoryRecord(
        id=memory_id,
        memory_type="user_profile",
        lifecycle="stable_memory",
        review_status="auto",
        visibility="shareable",
        content=f"{source}确认的画像事实",
        metadata=metadata,
        tags=["manual" if source == "manual" else "llm_tool"],
    )


def context(message: str = "聊点别的") -> SessionContext:
    return SessionContext(
        session_id="qq:FriendMessage:u1",
        scope="private",
        platform="qq",
        user_id="u1",
        bot_id="b1",
        message_text=message,
    )


class _MarkOnlyStore:
    async def mark_accessed(self, _memory_ids):
        return None


class _RankedEngine(RetrievalEngine):
    def __init__(self, ranked: list[SearchResult]):
        super().__init__(
            _MarkOnlyStore(),
            VisibilityPolicy(enable_acl_rules=False),
            retrieval_mode="basic",
            knowledge_graph_enabled=False,
        )
        self.ranked = ranked

    async def _rank_candidates(self, _query, _ctx, *, time_intent=None):
        return list(self.ranked), []


class ProfileRetrievalGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_profile_quality_and_state_gate_keeps_manual_compatibility(self) -> None:
        self.assertEqual(
            (True, "profile_quality_compatible"),
            RetrievalEngine._profile_retrieval_decision(rule_profile("active")),
        )

        allowed, reason = RetrievalEngine._profile_retrieval_decision(
            rule_profile("pending", state="candidate")
        )
        self.assertFalse(allowed)
        self.assertEqual("profile_state_candidate", reason)

        allowed, reason = RetrievalEngine._profile_retrieval_decision(
            rule_profile("low", score=0.4)
        )
        self.assertFalse(allowed)
        self.assertEqual("profile_quality_rejected", reason)

        manual = MemoryRecord(
            id="manual", memory_type="manual_memory", lifecycle="stable_memory"
        )
        self.assertEqual(
            (True, "profile_quality_compatible"),
            RetrievalEngine._profile_retrieval_decision(manual),
        )

        for source in ("manual", "tool_confirmed"):
            profile = compatible_profile(source, source)
            self.assertNotIn("extractor", profile.metadata)
            self.assertEqual(
                (True, "profile_quality_compatible"),
                RetrievalEngine._profile_retrieval_decision(profile),
            )

    def test_explicit_dimension_query_matches_structured_profile_metadata(self) -> None:
        engine = _RankedEngine([])
        query = "你还记得我的称呼吗？"
        terms = engine._terms(query)
        profile = engine._query_profile(query, terms)

        score, reason = engine._score(
            rule_profile("address"),
            terms,
            context(query),
            profile,
            {},
        )

        self.assertGreater(score, 0.0)
        self.assertIn("profile=1", reason)
        self.assertTrue(engine._query_allows_profile_fallback(query))
        self.assertFalse(engine._query_allows_profile_fallback("我该怎么称呼你？"))
        self.assertFalse(
            engine._query_blocks_profile_retrieval("你好呀，我的称呼是什么？")
        )

    def test_english_profile_intent_uses_word_boundaries(self) -> None:
        engine = _RankedEngine([])

        false_cases = (
            ("tell me about meeting notes", "preferred_address"),
            ("my jobless claim", "occupation"),
            ("call methods in Python", "preferred_address"),
        )
        for query, dimension in false_cases:
            profile = rule_profile(f"false-{dimension}")
            profile.metadata["profile_dimension"] = dimension
            with self.subTest(query=query, dimension=dimension):
                self.assertFalse(engine._query_allows_profile_fallback(query))
                self.assertFalse(engine._profile_query_matches_memory(query, profile))
                terms = engine._terms(query)
                _score, reason = engine._score(
                    profile,
                    terms,
                    context(query),
                    engine._query_profile(query, terms),
                    {},
                )
                self.assertNotIn("profile=1", reason)

        true_cases = (
            ("what is my name?", "preferred_address"),
            ("what should you call me?", "preferred_address"),
            ("what is my job?", "occupation"),
            ("tell me about me", "preferred_address"),
        )
        for query, dimension in true_cases:
            profile = rule_profile(f"true-{dimension}")
            profile.metadata["profile_dimension"] = dimension
            with self.subTest(query=query, dimension=dimension):
                self.assertTrue(engine._query_allows_profile_fallback(query))
                self.assertTrue(engine._profile_query_matches_memory(query, profile))

    async def test_ordinary_and_current_state_queries_do_not_get_profile_fallback(
        self,
    ) -> None:
        profile = SearchResult(
            memory=rule_profile("profile"),
            score=1.0,
            reason="hits=0;exact=0;profile=0;vector=0.100",
        )
        engine = _RankedEngine([profile])
        limits = {"user_profile": 2}

        for query in (
            "你好",
            "你好呀",
            "",
            "今天吃了什么？",
            "今天我的工作安排是什么？",
            "我的工作进度怎么样？",
            "关于我们的项目计划聊聊",
            "我的学业进度怎么样？",
            "my job progress",
            "tell me about meeting progress",
        ):
            results, _blocked, slots = await engine.search_by_slots(
                query,
                context(query),
                slot_limits=limits,
                total_limit=2,
            )
            self.assertEqual([], results, query)
            self.assertNotIn("user_profile", slots, query)

        results, _blocked, slots = await engine.search_by_slots(
            "你还记得我的称呼吗？",
            context("你还记得我的称呼吗？"),
            slot_limits=limits,
            total_limit=2,
        )
        self.assertEqual(["profile"], [item.memory.id for item in results])
        self.assertEqual(
            ["profile"], [item.memory.id for item in slots["user_profile"]]
        )

    async def test_current_task_query_keeps_lexically_relevant_profile(
        self,
    ) -> None:
        profile = SearchResult(
            memory=rule_profile("schedule-preference"),
            score=1.0,
            reason="hits=2;exact=1;profile=0;vector=0.100",
        )
        engine = _RankedEngine([profile])

        results, _blocked, slots = await engine.search_by_slots(
            "今天我的工作安排是什么？",
            context("今天我的工作安排是什么？"),
            slot_limits={"user_profile": 1},
            total_limit=1,
        )

        self.assertEqual(["schedule-preference"], [item.memory.id for item in results])
        self.assertEqual(
            ["schedule-preference"],
            [item.memory.id for item in slots["user_profile"]],
        )

    async def test_manual_profile_slot_remains_compatible_without_profile_intent(
        self,
    ) -> None:
        manual = MemoryRecord(
            id="manual",
            memory_type="manual_memory",
            lifecycle="stable_memory",
            visibility="shareable",
            content="人工确认的长期事实",
        )
        engine = _RankedEngine(
            [SearchResult(memory=manual, score=1.0, reason="hits=0;exact=0")]
        )

        results, _blocked, _slots = await engine.search_by_slots(
            "你好",
            context("你好"),
            slot_limits={"user_profile": 1},
            total_limit=1,
        )

        self.assertEqual(["manual"], [item.memory.id for item in results])


class ProfileInjectionGuardTests(unittest.TestCase):
    def test_tone_uses_abstract_hint_without_original_profile_text(self) -> None:
        composer = InjectionComposer()
        memory = rule_profile("tone", value="绝密昵称原文")
        result = SearchResult(memory=memory, score=1.0, reason="expression=tone")

        injection = composer.compose(context(), [result], max_chars=2400)

        self.assertIn("语气提示", injection)
        self.assertIn("禁止复述", injection)
        self.assertNotIn("绝密昵称原文", injection)
        self.assertIn(
            "injection_content_abstracted:tone", composer.last_omission_reasons
        )

    def test_candidate_expression_never_exposes_profile_source_text(self) -> None:
        composer = InjectionComposer()
        memory = rule_profile("candidate", value="候选私密称呼原文")
        result = SearchResult(
            memory=memory,
            score=1.0,
            reason="expression=candidate",
        )

        injection = composer.compose(context(), [result], max_chars=2400)

        self.assertEqual("", injection)
        self.assertIn(
            "injection_omitted:candidate",
            composer.last_omission_reasons,
        )

    def test_uncertain_and_low_quality_profiles_are_omitted_with_reasons(self) -> None:
        composer = InjectionComposer()
        uncertain = SearchResult(
            memory=rule_profile("uncertain", value="不应出现的称呼"),
            score=1.0,
            reason="expression=uncertain",
        )
        self.assertEqual("", composer.compose(context(), [uncertain], max_chars=2400))
        self.assertIn("injection_omitted:uncertain", composer.last_omission_reasons)

        low_quality = SearchResult(
            memory=rule_profile("low", value="错误称呼", score=0.4),
            score=1.0,
            reason="expression=mention",
        )
        self.assertEqual("", composer.compose(context(), [low_quality], max_chars=2400))
        self.assertIn(
            "injection_omitted:profile_quality_rejected", composer.last_omission_reasons
        )

    def test_qualified_mention_and_manual_memory_keep_fact_content(self) -> None:
        composer = InjectionComposer()
        qualified = SearchResult(
            memory=rule_profile("qualified", value="小王"),
            score=1.0,
            reason="expression=mention",
        )
        self.assertIn(
            "希望被称为 小王", composer.compose(context(), [qualified], max_chars=2400)
        )

        manual = MemoryRecord(
            id="manual",
            memory_type="manual_memory",
            lifecycle="stable_memory",
            visibility="shareable",
            content="人工确认事实",
            confidence=0.9,
        )
        injection = composer.compose(
            context(),
            [SearchResult(memory=manual, score=1.0, reason="expression=mention")],
            max_chars=2400,
        )
        self.assertIn("人工确认事实", injection)

        for source in ("manual", "tool_confirmed"):
            compatible = compatible_profile(source, source)
            injection = composer.compose(
                context(),
                [
                    SearchResult(
                        memory=compatible,
                        score=1.0,
                        reason="expression=mention",
                    )
                ],
                max_chars=2400,
            )
            self.assertIn(f"{source}确认的画像事实", injection)


class InjectionDiagnosticSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_survives_another_compose_after_await(self) -> None:
        composer = InjectionComposer()
        first = SearchResult(
            memory=rule_profile("first", value="不应出现", score=0.4),
            score=1.0,
            reason="expression=mention",
        )
        second = SearchResult(
            memory=rule_profile("second", value="小王"),
            score=1.0,
            reason="expression=mention",
        )
        first_composed = asyncio.Event()
        second_composed = asyncio.Event()

        async def first_request():
            composer.compose(context(), [first], max_chars=2400)
            snapshot = composer.diagnostic_snapshot()
            first_composed.set()
            await second_composed.wait()
            return snapshot

        async def second_request():
            await first_composed.wait()
            composer.compose(context(), [second], max_chars=2400)
            second_composed.set()

        first_snapshot, _ = await asyncio.gather(first_request(), second_request())
        omissions, included_ids = first_snapshot

        self.assertEqual(
            ["injection_omitted:profile_quality_rejected"],
            [item["reason"] for item in omissions],
        )
        self.assertEqual([], included_ids)
        self.assertEqual([], composer.last_omission_diagnostics)
        self.assertEqual(["second"], composer.last_included_memory_ids)


if __name__ == "__main__":
    unittest.main()
