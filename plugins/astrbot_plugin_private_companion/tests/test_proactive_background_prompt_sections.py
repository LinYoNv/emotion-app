from __future__ import annotations

import inspect
import unittest

from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptDocumentPart,
    PromptSection,
    prompt_cdata,
    prompt_section,
    render_prompt_document,
)
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class ProactiveBackgroundPromptSectionTests(unittest.TestCase):
    def render(self, document) -> str:
        return render_prompt_document(document)["user"]

    def assert_document(self, document, *, task: str) -> None:
        self.assertEqual(task, document.metadata["task"])
        self.assertFalse(document.system)
        self.assertTrue(document.user)
        self.assertTrue(all(isinstance(section, PromptSection) for section in document.user))
        self.assertTrue(
            all(isinstance(part, PromptDocumentPart) for part in document.user_parts)
        )
        keys = [section.key for section in document.user]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(keys))

    def test_screen_narration_document_keeps_body_only_wire(self) -> None:
        document = ProactiveMessageMixin._screen_narration_prompt_document(
            screen_term="屏幕",
            worldview_adaptation="世界观补充",
            cleaned_context="正在阅读文档",
        )

        self.assert_document(document, task="screen_narration")
        self.assertEqual(
            "请把下面的屏幕观察结果转成“视觉识别后的内部摘要”,供角色继续私聊使用。\n"
            "要求：\n"
            "1. 只描述视觉上看出来的内容,不要猜测工具调用过程,不要输出工具名、action 名、报错栈。\n"
            "2. 只概括用户大概正在看什么、做什么、情绪上是否像在忙,不要复述完整文字、账号、聊天原文、隐私细节。\n"
            "3. 绝对不要直接对用户说话,不要安慰、提醒、陪伴、劝休息,不要写成一条完整回复。\n"
            "4. 要像看了一眼屏幕后留在脑子里的印象,不要写成建议列表。\n"
            "5. 50 字以内,只输出摘要本身。\n\n"
            "世界观补充\n\n"
            "原始结果：\n"
            "正在阅读文档",
            self.render(document),
        )

    def test_reference_rewrite_document_preserves_legacy_sections_and_cdata(self) -> None:
        segmenting = prompt_section(
            key="reply.segmentation",
            title="回复分段控制",
            source="segmented_reply",
            content=prompt_cdata("A\n<<PRIVATE_COMPANION_SPLIT>>\nB"),
        )
        document = ProactiveMessageMixin._reference_rewrite_prompt_document(
            persona="人格正文",
            style_title="主动开口风格",
            reply_style="短句",
            history="用户：你好",
            recipient_identity="稳定 ID：QQ:1",
            scene="普通回执",
            reference="已经完成",
            creative_excerpt_rule="",
            status_rule="- 保留状态语义。",
            segmenting_section=segmenting,
        )
        rendered = self.render(document)

        self.assert_document(document, task="proactive_reference_rewrite")
        self.assertIn("【当前人格】\n人格正文", rendered)
        self.assertIn("【主动开口风格】\n短句", rendered)
        self.assertIn("要求：\n- 只输出最终聊天正文", rendered)
        self.assertTrue(rendered.endswith("</private_companion_context>"))
        self.assertIn("<![CDATA[A\n<<PRIVATE_COMPANION_SPLIT>>\nB]]>", rendered)

    def test_proactive_send_review_document_uses_square_headings_and_json_contract(self) -> None:
        document = ProactiveMessageMixin._proactive_send_review_prompt_document(
            creative_excerpt_section=None,
            history="history",
            runtime_context="runtime",
            troubleshooting_context="request",
            fact_source_context="fact",
            local_context="local",
            source_context="source",
            route_review_directive="route",
            persona_context="persona",
            intent_hint="intent",
            proactive_voice="voice",
            expression_voice="expression",
            recipient_identity="recipient",
            candidate="candidate",
        )
        rendered = self.render(document)

        self.assert_document(document, task="proactive_send_review")
        self.assertIn("[Recent conversation]\nhistory", rendered)
        self.assertIn("[Recipient identity boundary]\nrecipient", rendered)
        self.assertTrue(
            rendered.endswith(
                'Output:\n{"decision":"send|rewrite|drop","text":"","reason":"brief reason"}'
            )
        )

    def test_response_review_document_keeps_legacy_headings(self) -> None:
        document = ProactiveMessageMixin._response_review_prompt_document(
            original_text="原文",
            flags="reply_air_opener",
            reason="check_in",
            motive="想说话",
            topic="近况",
            action_context="无工具结果",
            intent_hint="低压力",
            persona="人格",
            proactive_voice="短句",
            expression_voice="自然",
            recipient_identity="当前用户",
            creative_excerpt_rule="",
        )
        rendered = self.render(document)

        self.assert_document(document, task="response_review")
        self.assertIn("【原主动消息】\n原文", rendered)
        self.assertIn("【动机/话题】\n想说话\n近况", rendered)
        self.assertIn("要求：\n- 只输出要发送的正文", rendered)

    def test_voice_documents_keep_tts_fields_and_output_rules(self) -> None:
        framework = ProactiveMessageMixin._framework_voice_prompt_document(
            name="小林",
            reason="quiet_care",
            last_user_message="晚点聊",
            relationship_level="close",
            relationship_preference="轻声",
            state_hint="平稳",
            busy_hint="忙碌",
            tts_prompt="<tts>日语</tts>",
            requirement_summary="必须保留标签",
            strict_tts=True,
        )
        fallback = ProactiveMessageMixin._voice_fallback_prompt_document(
            persona="人格",
            name="小林",
            relationship_level="close",
            relationship_preference="轻声",
            last_user_message="晚点聊",
            reason="quiet_care",
            state="平稳",
            tts_prompt="<tts>日语</tts>",
            max_chars=30,
        )
        repair = ProactiveMessageMixin._voice_repair_prompt_document(
            persona="人格",
            tts_prompt="<tts>日语</tts>",
            requirement_summary="必须保留标签",
            spoken="你好",
        )

        for document, task in (
            (framework, "proactive_voice"),
            (fallback, "voice"),
            (repair, "voice_repair"),
        ):
            with self.subTest(task=task):
                self.assert_document(document, task=task)
                rendered = self.render(document)
                self.assertIn("<tts>日语</tts>", rendered)
                self.assertNotIn("【主动语音", rendered)
        self.assertIn("6. 这次必须优先满足语音格式要求", self.render(framework))
        self.assertIn("【当前版本】\n你好", self.render(repair))

    def test_photo_documents_keep_json_and_candidate_contracts(self) -> None:
        relationship = prompt_section(
            key="background.photo_scene.relationships",
            title="Bot 关系网",
            source="proactive_message",
            content="- 角色：Yui",
        )
        scene = ProactiveMessageMixin._photo_scene_generation_prompt_document(
            persona="人格",
            recipient_name="小林",
            scene_context="客厅",
            topic_hint="咖啡",
            motive_hint="想分享",
            relationship_section=relationship,
            birthday_rule="（非生日卡）",
            content_options="桌面小物",
            style_name="真实",
            style_instruction="自然光",
            prompt_format_instruction="英文自然语言",
            reason="activity_share",
        )
        selection = ProactiveMessageMixin._photo_reference_selection_prompt_document(
            request_text="穿睡衣坐在沙发上",
            ambient_context="家里",
            suggested_scene_preset="home",
            schedule_history_context="刚回家",
            candidate_options="1. id=home",
        )
        intent = ProactiveMessageMixin._photo_reference_intent_prompt_document(
            "只参考人物身份，不参考服装"
        )

        self.assert_document(scene, task="photo_prompt")
        self.assert_document(selection, task="photo_reference_selection")
        self.assert_document(intent, task="photo_reference_intent")
        scene_text = self.render(scene)
        self.assertIn("【Bot 关系网】\n- 角色：Yui", scene_text)
        self.assertIn('输出 JSON：\n{\n  "kind":', scene_text)
        self.assertIn("【候选参考图】\n1. id=home", self.render(selection))
        self.assertIn(
            '{"requested_roles":[],"excluded_roles":[],"continuity_mode":"ambiguous","confidence":0.0}',
            self.render(intent),
        )

    def test_screen_skill_documents_are_body_only(self) -> None:
        goodnight = ProactiveMessageMixin._goodnight_screen_check_prompt_document()
        peek = ProactiveMessageMixin._screen_peek_prompt_document("quiet_care")

        self.assert_document(goodnight, task="goodnight_screen_check")
        self.assert_document(peek, task="screen_peek")
        self.assertTrue(self.render(goodnight).startswith("这是一次用户已授权"))
        self.assertTrue(self.render(peek).endswith("主动原因：quiet_care"))
        self.assertEqual(
            "晚安后单次确认 小林 是否仍在主动使用电脑。",
            ProactiveMessageMixin._goodnight_screen_check_history_text("小林"),
        )
        self.assertEqual(
            "主动陪伴想轻轻看一眼 小林 现在在忙什么。",
            ProactiveMessageMixin._screen_peek_history_text("小林"),
        )

    def test_recent_context_compatibility_wrappers_have_one_legacy_wire(self) -> None:
        harness = ProactiveMessageMixin()
        cases = (
            (
                "_format_recent_web_exploration_context_for_reply",
                "_format_recent_web_exploration_context_prompt_section",
                "web_exploration.recent",
                "主动搜索上下文",
            ),
            (
                "_format_recent_ai_daily_context_for_reply",
                "_format_recent_ai_daily_context_prompt_section",
                "news.ai_daily_context",
                "新闻阅读上下文",
            ),
            (
                "_format_recent_news_context_for_reply",
                "_format_recent_news_context_prompt_section",
                "news.recent",
                "新闻阅读上下文",
            ),
        )

        for wrapper_name, producer_name, key, title in cases:
            section = prompt_section(
                key=key,
                title=title,
                source="test",
                content="真实上下文",
            )
            setattr(harness, producer_name, lambda _text="", value=section: value)
            wrapper = getattr(harness, wrapper_name)
            with self.subTest(wrapper=wrapper_name):
                self.assertEqual(f"【{title}】\n真实上下文", wrapper("用户问题"))
                self.assertNotIn("as_section", inspect.signature(wrapper).parameters)

    def test_background_prompt_producers_do_not_author_legacy_brackets(self) -> None:
        producers = (
            ProactiveMessageMixin._screen_narration_prompt_document,
            ProactiveMessageMixin._reference_rewrite_prompt_document,
            ProactiveMessageMixin._proactive_send_review_prompt_document,
            ProactiveMessageMixin._response_review_prompt_document,
            ProactiveMessageMixin._framework_voice_prompt_document,
            ProactiveMessageMixin._voice_fallback_prompt_document,
            ProactiveMessageMixin._voice_repair_prompt_document,
            ProactiveMessageMixin._photo_scene_generation_prompt_document,
            ProactiveMessageMixin._photo_reference_selection_prompt_document,
            ProactiveMessageMixin._photo_reference_intent_prompt_document,
            ProactiveMessageMixin._goodnight_screen_check_prompt_document,
            ProactiveMessageMixin._goodnight_screen_check_history_text,
            ProactiveMessageMixin._screen_peek_prompt_document,
            ProactiveMessageMixin._screen_peek_history_text,
        )

        for producer in producers:
            with self.subTest(producer=producer.__name__):
                self.assertNotIn("【", inspect.getsource(producer))

    def test_proactive_prompt_documents_use_only_typed_render_contracts(self) -> None:
        source = inspect.getsource(ProactiveMessageMixin)

        for legacy_key in (
            "legacy_render_mode",
            "legacy_heading_style",
            "legacy_prefix",
            "legacy_separator_before",
            "_render_proactive_prompt_document",
        ):
            with self.subTest(legacy_key=legacy_key):
                self.assertNotIn(legacy_key, source)


if __name__ == "__main__":
    unittest.main()
