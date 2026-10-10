import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_private_companion.daily_state import DailyStateMixin
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class _PersonaManager:
    async def get_default_persona_v3(self, umo=""):
        await asyncio.sleep(0)
        return {"prompt": f"persona-for:{umo}"}


class _BrokenPersonaManager:
    async def get_default_persona_v3(self, umo=""):
        raise sqlite3.OperationalError("no active connection")


class _CancelledPersonaManager:
    async def get_default_persona_v3(self, umo=""):
        raise asyncio.CancelledError()


class _PersonaCacheHarness(DailyStateMixin):
    def __init__(self, manager=None):
        self.plugin_specific_persona_id = ""
        self.context = SimpleNamespace(persona_manager=manager or _PersonaManager())
        self._default_persona_prompt_cache = ""
        self._default_persona_prompt_cache_at = 0.0
        self._default_persona_prompt_cache_umo = ""
        self._default_persona_prompt_cache_persona_id = ""
        self._default_persona_prompt_cache_by_scope = {}


class _ProactivePersonaHarness(ProactiveMessageMixin, DailyStateMixin):
    schedule_persona_prompt = "高一学生"
    schedule_worldview_prompt = "现代校园"

    def __init__(self):
        self.enable_proactive_message_review = True
        self.enable_passive_response_review = False
        self.enable_response_self_review = False
        self.proactive_review_mode = "full"
        self.response_review_provider_id = "review"
        self.mai_style_provider_id = "style"
        self.persona_proactive_voice_prompt = "开头常用：嗯…\n保持克制但有一点俏皮。"
        self.proactive_review_history_limit = 27
        self.captured_prompt = ""
        self.history_limits = []
        self.resolved_sessions = []

    async def _refresh_default_persona_prompt(self, umo=""):
        self.resolved_sessions.append(umo)
        return f"完整人格::{umo}::冷静、俏皮、称呼稳定"

    def _get_default_persona_prompt(self, umo=""):
        return f"fallback::{umo}"

    def _local_proactive_send_decision(self, *args, **kwargs):
        return {"decision": "send", "reason": "本地通过"}

    def _normalize_proactive_review_decision_policy(self, user, payload, **kwargs):
        return dict(payload)

    async def _recent_private_conversation_for_proactive_review(self, user, *, limit=10):
        self.history_limits.append(limit)
        return "用户: 最近在看书"

    def _format_proactive_generation_intent_hint(self, *args, **kwargs):
        return "保持低压力分享"

    def _format_proactive_voice_prompt(self):
        return "主动风格：嗯…开头，俏皮但不黏人"

    def _format_proactive_recipient_identity_guard(self, user, name=""):
        return f"当前收件人：{name or '你'}；普通朋友边界"

    def _format_proactive_review_runtime_context(self, user, *, now=None):
        return "当前适合轻量开口"

    def _proactive_review_strength(self):
        return "lenient"

    def _task_provider(self, *values):
        return next((value for value in values if value), "")

    async def _llm_call(self, prompt, **kwargs):
        self.captured_prompt = prompt
        if kwargs.get("task") == "proactive_send_review":
            return json.dumps({"decision": "send", "text": "", "reason": "符合人格"}, ensure_ascii=False)
        return "嗯…这页看完记得歇一下。"

    @staticmethod
    def _parse_json_object(raw):
        return json.loads(raw)

    @staticmethod
    def _sanitize_action_boundaries(text, **kwargs):
        return text

    @staticmethod
    def _sanitize_proactive_text(text):
        return str(text or "").strip()

    @staticmethod
    def _normalize_proactive_sentence_flow(text):
        return text

    @staticmethod
    def _framework_agent_meta_summary_leak(text):
        return False

    @staticmethod
    def _repair_proactive_recipient_address(text, user, name=""):
        return text, ""

    def _proactive_reply_air_flags(self, text, **kwargs):
        return ["回复式开场"] if text == "原始候选" else []

    @staticmethod
    def _strip_parenthetical_stage_directions(text):
        return text

    @staticmethod
    def _trim_proactive_status_inventory(text):
        return text

    @staticmethod
    def _trim_performative_self_state_tail(text):
        return text


class _SourceCorruptingReviewHarness(_ProactivePersonaHarness):
    async def _llm_call(self, prompt, **kwargs):
        self.captured_prompt = prompt
        if kwargs.get("task") == "proactive_send_review":
            return json.dumps(
                {
                    "decision": "rewrite",
                    "text": "刚刷到 B站这个视频，想给你看看。https://www.reddit.com/r/example/comments/test-post/",
                    "reason": "更自然",
                },
                ensure_ascii=False,
            )
        return await super()._llm_call(prompt, **kwargs)


class _DegenerateReviewHarness(_ProactivePersonaHarness):
    async def _llm_call(self, prompt, **kwargs):
        self.captured_prompt = prompt
        if kwargs.get("task") == "proactive_send_review":
            return json.dumps(
                {"decision": "rewrite", "text": "哦。", "reason": "更简短"},
                ensure_ascii=False,
            )
        return await super()._llm_call(prompt, **kwargs)


class _LocalDeferReviewHarness(_ProactivePersonaHarness):
    def _local_proactive_send_decision(self, *args, **kwargs):
        return {
            "decision": "defer",
            "reason": "由头偏虚且打扰压力较高",
            "delay_minutes": 75,
        }


class _DropReviewHarness(_ProactivePersonaHarness):
    async def _llm_call(self, prompt, **kwargs):
        self.captured_prompt = prompt
        if kwargs.get("task") == "proactive_send_review":
            return json.dumps(
                {"decision": "drop", "text": "", "reason": "和当前对话冲突"},
                ensure_ascii=False,
            )
        return await super()._llm_call(prompt, **kwargs)


class ProactivePersonaConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_persona_cache_is_isolated_by_session(self):
        harness = _PersonaCacheHarness()
        first, second = await asyncio.gather(
            harness._refresh_default_persona_prompt("session-a"),
            harness._refresh_default_persona_prompt("session-b"),
        )

        self.assertEqual(first, "persona-for:session-a")
        self.assertEqual(second, "persona-for:session-b")
        self.assertEqual(harness._get_default_persona_prompt("session-a"), "persona-for:session-a")
        self.assertEqual(harness._get_default_persona_prompt("session-b"), "persona-for:session-b")

    async def test_persona_database_connection_error_uses_fallback(self):
        harness = _PersonaCacheHarness(_BrokenPersonaManager())

        self.assertEqual(
            harness._get_default_persona_prompt("session-a"),
            "未读取到 AstrBot 默认人格。请保持简洁、温和、有边界,不额外创造新身份。",
        )
        result = await harness._refresh_default_persona_prompt("session-a")

        self.assertEqual(result, harness._get_default_persona_prompt("session-a"))

    async def test_internal_persona_lookup_cancellation_uses_fallback(self):
        harness = _PersonaCacheHarness(_CancelledPersonaManager())

        result = await harness._refresh_default_persona_prompt("session-a")

        self.assertEqual(result, harness._get_default_persona_prompt("session-a"))

    async def test_final_send_review_receives_full_persona(self):
        harness = _ProactivePersonaHarness()
        user = {"umo": "session-a", "nickname": "小林", "user_id": "1"}

        self.assertFalse(harness.enable_passive_response_review)
        self.assertTrue(harness.enable_proactive_message_review)

        result = await harness._review_proactive_message_send_decision(
            user,
            "窗边那页书看得有点久啦。",
            reason="quiet_care",
            action="message",
            motive="顺手提醒休息",
            topic="看书",
        )

        self.assertEqual(result["decision"], "send")
        self.assertTrue(result["review_model_ok"])
        self.assertIn("完整人格::session-a::冷静、俏皮、称呼稳定", harness.captured_prompt)
        self.assertIn("主动风格：嗯…开头，俏皮但不黏人", harness.captured_prompt)
        self.assertIn("当前收件人：小林；普通朋友边界", harness.captured_prompt)
        self.assertEqual([27], harness.history_limits)

    async def test_troubleshooting_review_treats_user_request_as_real_reason(self):
        harness = _ProactivePersonaHarness()
        user = {
            "umo": "session-test",
            "nickname": "小林",
            "user_id": "1",
            "planned_proactive_source": "troubleshooting",
        }

        result = await harness._review_proactive_message_send_decision(
            user,
            "夜深了，轻轻来和你说句话。",
            reason="check_in",
            action="message",
            motive="用户明确希望测试一次主动消息",
            topic="自然来找对方一下",
        )

        self.assertEqual("send", result["decision"])
        self.assertIn("用户刚刚在控制面板明确发起了一次主动消息链路测试", harness.captured_prompt)
        self.assertIn("Do not drop solely because it is late", harness.captured_prompt)

    async def test_final_review_keeps_complete_candidate_when_rewrite_becomes_filler(self):
        harness = _DegenerateReviewHarness()
        original = "暴雨预警又升级啦，你已经平安到家了吗？"

        result = await harness._review_proactive_message_send_decision(
            {"umo": "session-weather", "nickname": "小林", "user_id": "1"},
            original,
            reason="weather_alert",
            action="message",
            motive="确认回家安全",
            topic="暴雨预警升级",
        )

        self.assertEqual("send", result["decision"])
        self.assertEqual("", result["text"])
        self.assertIn("Never collapse", harness.captured_prompt)

    async def test_model_send_preserves_local_timing_defer(self):
        harness = _LocalDeferReviewHarness()

        result = await harness._review_proactive_message_send_decision(
            {"umo": "session-defer", "nickname": "小林", "user_id": "1"},
            "刚刚想到一件小事，想顺手和你说一句。",
            reason="check_in",
            action="message",
        )

        self.assertEqual("defer", result["decision"])
        self.assertEqual(75, result["delay_minutes"])
        self.assertIn("本地时机判断", result["reason"])

    async def test_model_failure_marks_local_release_for_runtime_alerting(self):
        harness = _ProactivePersonaHarness()
        harness._llm_call = AsyncMock(side_effect=RuntimeError("review provider unavailable"))

        result = await harness._review_proactive_message_send_decision(
            {"umo": "session-fallback", "nickname": "小林", "user_id": "1"},
            "窗边那页书看得有点久啦。",
            reason="quiet_care",
            action="message",
        )

        self.assertEqual("send", result["decision"])
        self.assertTrue(result["review_fallback"])
        self.assertIn("provider unavailable", result["review_fallback_reason"])
        self.assertNotIn("review_model_ok", result)

    async def test_model_drop_marks_review_provider_as_recovered(self):
        harness = _DropReviewHarness()

        result = await harness._review_proactive_message_send_decision(
            {"umo": "session-drop", "nickname": "小林", "user_id": "1"},
            "现在突然换个完全无关的话题。",
            reason="check_in",
            action="message",
        )

        self.assertEqual("drop", result["decision"])
        self.assertTrue(result["review_model_ok"])

    async def test_final_review_cannot_replace_verified_bilibili_link(self):
        harness = _SourceCorruptingReviewHarness()
        user = {
            "umo": "session-bili",
            "nickname": "小林",
            "user_id": "1",
            "bilibili_video_context": {
                "title": "春季新番预告",
                "bvid": "BV1AB411C7mD",
                "comment": "这个片段很轻松",
            },
        }
        original = "刚刷到 B站《春季新番预告》，这个片段很轻松。https://www.bilibili.com/video/BV1AB411C7mD"

        result = await harness._review_proactive_message_send_decision(
            user,
            original,
            reason="bili_video_share",
            action="message",
            motive="想分享刚看到的视频",
            topic="春季新番预告",
        )

        self.assertEqual(result["decision"], "rewrite")
        self.assertEqual(result["text"], original)
        self.assertNotIn("reddit.com", result["text"])

    async def test_reply_air_rewrite_keeps_persona_context(self):
        harness = _ProactivePersonaHarness()
        user = {"umo": "session-b", "nickname": "阿青", "planned_proactive_topic": "看书"}

        result = await harness._review_proactive_message_stance(
            user,
            "原始候选",
            reason="quiet_care",
            action="message",
            motive="提醒休息",
        )

        self.assertTrue(result.startswith("嗯…"))
        self.assertIn("完整人格::session-b::冷静、俏皮、称呼稳定", harness.captured_prompt)
        self.assertIn("不得把原文改成另一种人格", harness.captured_prompt)

    async def test_disabled_review_repairs_soft_reply_air_instead_of_dropping(self):
        harness = _ProactivePersonaHarness()
        harness.enable_proactive_message_review = False
        harness._llm_call = AsyncMock(side_effect=AssertionError("disabled review must not call the model"))
        harness._proactive_reply_air_flags = lambda text, **kwargs: ProactiveMessageMixin._proactive_reply_air_flags(
            harness,
            text,
            **kwargs,
        )
        user = {"umo": "session-b", "nickname": "阿青"}

        result = await harness._review_proactive_message_stance(
            user,
            "好呀，今天记得早点休息。",
            reason="quiet_care",
            action="message",
            motive="提醒休息",
        )

        self.assertEqual("今天记得早点休息。", result)
        harness._llm_call.assert_not_awaited()

    async def test_disabled_review_releases_non_hard_local_defer_and_drop_without_model(self):
        for decision in ("defer", "drop"):
            with self.subTest(decision=decision):
                harness = _ProactivePersonaHarness()
                harness.enable_proactive_message_review = False
                harness._llm_call = AsyncMock(side_effect=AssertionError("disabled review must not call the model"))
                harness._local_proactive_send_decision = lambda *_args, **_kwargs: {
                    "decision": decision,
                    "reason": "表达温度偏低，建议收短",
                    "delay_minutes": 60,
                }

                result = await harness._review_proactive_message_send_decision(
                    {"umo": "session-soft", "nickname": "阿青", "user_id": "1"},
                    "刚把桌边散着的笔收好了，顺手来和你说一句。",
                    reason="state_share",
                    action="message",
                    motive="分享眼前的小事",
                    topic="收拾书桌",
                )

                self.assertEqual("send", result["decision"])
                self.assertIn("跳过非安全性的本地软拦截", result["reason"])
                harness._llm_call.assert_not_awaited()

    async def test_disabled_review_keeps_hard_local_risk_blocked_without_model(self):
        harness = _ProactivePersonaHarness()
        harness.enable_proactive_message_review = False
        harness._llm_call = AsyncMock(side_effect=AssertionError("hard local risk must not call the model"))
        harness._local_proactive_send_decision = lambda *_args, **_kwargs: {
            "decision": "defer",
            "reason": "用户明确要求休息，继续发送会构成骚扰",
            "delay_minutes": 180,
            "hard": True,
        }

        result = await harness._review_proactive_message_send_decision(
            {"umo": "session-hard", "nickname": "阿青", "user_id": "1"},
            "再陪我聊一会吧。",
            reason="check_in",
            action="message",
            motive="继续找对方聊天",
            topic="续聊",
        )

        # A hard local risk may be represented as either defer or drop by the
        # surrounding policy adapter; it must never be released as send.
        self.assertIn(result["decision"], {"defer", "drop"})
        self.assertNotEqual("send", result["decision"])
        self.assertTrue(result["hard"])
        harness._llm_call.assert_not_awaited()

    async def test_proactive_review_does_not_reuse_an_unverified_mother(self):
        harness = _ProactivePersonaHarness()
        user = {
            "umo": "session-b",
            "nickname": "阿青",
            "planned_proactive_topic": "妈妈洗了青提，后来翻开练习册",
        }

        await harness._review_proactive_message_stance(
            user,
            "原始候选",
            reason="activity_share",
            action="message",
            motive="被妈妈提醒后，想说已经把物理题写完了",
        )

        self.assertNotIn("妈妈", harness.captured_prompt)
        self.assertIn("后来翻开练习册", harness.captured_prompt)
        self.assertIn("已经把物理题写完", harness.captured_prompt)

    async def test_resolver_uses_returned_session_persona(self):
        harness = _ProactivePersonaHarness()
        resolved = await harness._resolve_proactive_persona_prompt({"umo": "session-c"})
        self.assertEqual(resolved, "完整人格::session-c::冷静、俏皮、称呼稳定")
        self.assertEqual(harness.resolved_sessions, ["session-c"])

    def test_configured_signature_opening_is_not_removed(self):
        harness = _ProactivePersonaHarness()
        user = {"action_consequences": [{"text": "嗯…昨晚那本书还挺有意思。"}]}
        text = "嗯…窗边那页看完就歇一下。"
        self.assertEqual(harness._apply_proactive_style_variation(text, user), text)

    def test_framework_conversation_uses_plugin_persona_without_mutating_original(self):
        harness = _ProactivePersonaHarness()
        harness.plugin_specific_persona_id = "plugin-persona"
        original = SimpleNamespace(persona_id="conversation-persona", history="[]")

        scoped = harness._proactive_conversation_with_configured_persona(original)

        self.assertIsNot(scoped, original)
        self.assertEqual(scoped.persona_id, "plugin-persona")
        self.assertEqual(original.persona_id, "conversation-persona")

    def test_persona_phrase_is_not_hard_deleted(self):
        harness = _ProactivePersonaHarness()
        text = "突然想起你，窗外那阵雨刚停。"
        self.assertIn("突然想起你", harness._soften_social_proactive_text(text))

    def test_visible_proactive_body_does_not_inherit_tts_script_format(self):
        hint = _ProactivePersonaHarness._proactive_visible_text_format_hint("photo_text")

        self.assertIn("最终显示在聊天里的普通正文", hint)
        self.assertIn("图片动作写可见附言", hint)
        self.assertIn("[happy]/[sad]", hint)
        self.assertIn("不能仅凭 TTS 语种要求切换", hint)


if __name__ == "__main__":
    unittest.main()
