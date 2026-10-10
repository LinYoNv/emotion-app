# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest
from unittest.mock import patch

from astrbot_plugin_private_companion.group_observation import GroupObservationMixin
from astrbot_plugin_private_companion.group_wakeup import GroupWakeupMixin
from astrbot_plugin_private_companion.forward_message import ForwardMessageMixin
from astrbot_plugin_private_companion.helpers import _group_link_message_context


class _InterjectionHarness(GroupObservationMixin):
    enable_group_interjection = True


class _RepeatHarness(_InterjectionHarness):
    enable_group_repeat_follow = True
    group_repeat_count_distinct_users_only = False
    group_repeat_trigger_threshold = 3
    group_repeat_follow_probability = 1.0
    group_repeat_interrupt_probability = 0.0
    group_repeat_interrupt_probability_step = 0.0
    group_repeat_interrupt_image_path = ""
    group_repeat_interrupt_text = "禁止复读"
    group_interject_max_daily = 12

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []

    @staticmethod
    def _compact_repeat_text(text: str) -> str:
        return str(text or "").strip()

    @staticmethod
    def _group_topic_signature(text: str) -> str:
        return str(text or "").strip()

    async def _reply_with_optional_media(
        self,
        _event,
        text: str,
        *,
        image_path: str = "",
        quote_message_id: str = "",
    ) -> None:
        self.replies.append((text, image_path))

    def _group_interjection_allowed(self, group, text):
        raise AssertionError("general interjection must stay disabled in high-intensity mode")


class _WakeupHarness(GroupWakeupMixin):
    enable_group_wakeup_enhancement = True

    def __init__(self) -> None:
        self.data = {}
        self.web_exploration_interests = ""
        self.group_wakeup_interest_keywords = []
        self.group_wakeup_context_words = []
        self.group_wakeup_generated_keyword_limit = 24
        self.group_wakeup_interest_probability = 0.2
        self.group_wakeup_cooldown_seconds = 0
        self.group_wakeup_fatigue_limit = 100
        self.group_wakeup_fatigue_decay_minutes = 10
        self.group_wakeup_log_limit = 80
        self.enable_group_wakeup_question = False
        self.enable_group_wakeup_cold_group = False

    @staticmethod
    def _parse_text_list_config(value, *, limit=40):
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()][:limit]
        return [item.strip() for item in str(value or "").split(",") if item.strip()][:limit]

    @staticmethod
    def _group_wakeup_topic_interest_weight(group, word, *, sender_id, text, group_id):
        return {"multiplier": 1.0, "score": 0.0, "reason": ""}

    @staticmethod
    def _select_worldbook_member_profiles_for_group(group, *, sender_id, text):
        return []


class _QuotedLinkHarness(ForwardMessageMixin):
    def __init__(self, rows):
        self.rows = rows

    @staticmethod
    def _event_has_reply_component(event) -> bool:
        return True

    async def _reply_message_chain_for_event(self, event, *, max_depth=3):
        return self.rows[:max_depth]


class _WakeEvent:
    is_wake = True

    def get_sender_id(self) -> str:
        raise AssertionError("wakeup messages must not enter the proactive interjection path")


class _RepeatEvent:
    private_companion_group_quoted_link_payload = False

    def __init__(self, *, sender_id: str = "10001", wake: bool = False) -> None:
        self.sender_id = sender_id
        self.is_wake = wake
        self.is_at_or_wake_command = wake
        self.stopped = False

    def get_sender_id(self) -> str:
        return self.sender_id

    def stop_event(self) -> None:
        self.stopped = True


class GroupLinkReplyGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.interjection = _InterjectionHarness()
        self.wakeup = _WakeupHarness()
        self.wakeup.bot_name = ""
        self.wakeup.group_wakeup_direct_words = []

    def test_url_query_question_mark_is_not_user_question(self) -> None:
        url = "https://www.smzdm.com/p/test-item/?from=other&invite_code=test"
        non_link_text, has_link = _group_link_message_context(url)

        self.assertTrue(has_link)
        self.assertEqual(non_link_text, "")
        self.assertEqual(self.wakeup._group_wakeup_question_signal(url), {})

    def test_bare_link_and_share_card_cannot_trigger_interjection(self) -> None:
        for text in (
            "https://example.com/item?id=1",
            "[分享] https://example.com/item?id=1",
            "【QQ小程序】商品分享",
        ):
            allowed, reason = self.interjection._group_interjection_allowed({}, text)
            self.assertFalse(allowed)
            self.assertIn("链接或分享", reason)

    def test_bare_link_cannot_trigger_other_group_wakeup_sources(self) -> None:
        wakeup = self.wakeup._evaluate_group_wakeup(
            {},
            scene={"talking_to": "group", "trigger": "normal"},
            sender_id="10001",
            sender_name="群友",
            text="https://example.com/bot/interesting-topic?id=1",
            group_id="20001",
        )

        self.assertEqual(wakeup, {})

    def test_qq_compatibility_boilerplate_is_treated_as_share_payload(self) -> None:
        for text in (
            "当前QQ版本不支持此应用，请升级",
            "当前 QQ 版本不支持该应用, 请升级",
            "您的QQ版本过低，请升级后查看",
            "请使用最新版本手机QQ查看",
        ):
            non_link_text, has_link = _group_link_message_context(text)

            self.assertTrue(has_link)
            self.assertEqual(non_link_text, "")

    def test_normal_qq_version_discussion_is_not_treated_as_share_payload(self) -> None:
        text = "我的QQ版本过低，怎么升级？"
        non_link_text, has_link = _group_link_message_context(text)

        self.assertFalse(has_link)
        self.assertEqual(non_link_text, text)

    def test_qq_compatibility_boilerplate_cannot_trigger_interest_wakeup(self) -> None:
        self.wakeup.group_wakeup_interest_keywords = ["升级"]

        wakeup = self.wakeup._evaluate_group_wakeup(
            {},
            scene={"talking_to": "group", "trigger": "normal"},
            sender_id="10001",
            sender_name="群友",
            text="当前QQ版本不支持此应用，请升级",
            group_id="20001",
        )

        self.assertEqual(wakeup, {})

    def test_interest_probability_miss_blocks_general_interjection_fallthrough(self) -> None:
        self.wakeup.group_wakeup_interest_keywords = ["摄影"]
        scene = {"talking_to": "group", "trigger": "normal"}

        with patch("astrbot_plugin_private_companion.group_wakeup.random.random", return_value=0.9):
            wakeup = self.wakeup._evaluate_group_wakeup(
                {},
                scene=scene,
                sender_id="10001",
                sender_name="群友",
                text="今天出去拍摄影素材",
                group_id="20001",
            )

        self.assertEqual(wakeup, {})
        miss = scene.get("interest_keyword_probability_miss")
        self.assertIsInstance(miss, dict)
        self.assertEqual(miss.get("words"), ["摄影"])
        self.assertFalse(self.wakeup._group_wakeup_allows_general_interjection(scene))

    def test_weak_wakeup_word_alone_does_not_trigger_reply(self) -> None:
        self.wakeup.group_wakeup_context_words = ["机器人"]
        wakeup = self.wakeup._evaluate_group_wakeup(
            {},
            scene={"talking_to": "group", "trigger": "normal"},
            sender_id="10001",
            sender_name="群友",
            text="机器人呢？",
            group_id="20001",
        )

        self.assertEqual(wakeup, {})

    def test_weak_wakeup_word_with_clear_bot_directed_request_can_trigger(self) -> None:
        self.wakeup.group_wakeup_context_words = ["机器人"]
        wakeup = self.wakeup._evaluate_group_wakeup(
            {},
            scene={"talking_to": "group", "trigger": "normal"},
            sender_id="10001",
            sender_name="群友",
            text="机器人你怎么看这个方案？",
            group_id="20001",
        )

        self.assertEqual(wakeup.get("type"), "context_word")

    def test_question_beside_qq_boilerplate_keeps_only_user_text(self) -> None:
        text = "这个视频讲什么？ 当前QQ版本不支持此应用，请升级"
        non_link_text, has_link = _group_link_message_context(text)

        self.assertTrue(has_link)
        self.assertEqual(non_link_text, "这个视频讲什么？")
        signal = self.wakeup._group_wakeup_question_signal(text)
        self.assertTrue(signal)
        self.assertEqual(signal.get("raw_text"), "这个视频讲什么？")

    def test_qq_compatibility_boilerplate_cannot_trigger_interjection(self) -> None:
        allowed, reason = self.interjection._group_interjection_allowed(
            {}, "当前QQ版本不支持此应用，请升级"
        )

        self.assertFalse(allowed)
        self.assertIn("链接或分享", reason)

    def test_question_beside_link_does_not_wake_without_addressing_bot(self) -> None:
        wakeup = self.wakeup._evaluate_group_wakeup(
            {},
            scene={"talking_to": "group", "trigger": "normal"},
            sender_id="10001",
            sender_name="群友",
            text="这个测试商品值得买吗？ https://www.smzdm.com/p/test-item/?from=other",
            group_id="20001",
        )

        self.assertEqual(wakeup, {})

    def test_direct_bot_name_can_still_override_current_link_guard(self) -> None:
        self.wakeup.bot_name = "测试角色"
        self.wakeup.group_wakeup_direct_words = []
        wakeup = self.wakeup._evaluate_group_wakeup(
            {},
            scene={"talking_to": "group", "trigger": "normal"},
            sender_id="10001",
            sender_name="群友",
            text="测试角色看看这个 https://example.com/item",
            group_id="20001",
        )

        self.assertEqual(wakeup.get("type"), "direct_word")

    async def test_quoted_plain_link_is_detected(self) -> None:
        harness = _QuotedLinkHarness(
            [{"raw_message": "https://example.com/item?id=1", "text": "https://example.com/item?id=1"}]
        )
        self.assertTrue(await harness._event_reply_contains_link_payload(object()))

    async def test_quoted_share_card_link_is_detected_but_image_only_is_not(self) -> None:
        card = _QuotedLinkHarness(
            [{"raw_message": {"type": "json", "data": {"title": "商品", "jumpUrl": "https://example.com/item"}}, "text": "商品"}]
        )
        image = _QuotedLinkHarness(
            [{"raw_message": {"type": "image", "data": {"url": "https://cdn.example.com/a.jpg"}}, "text": "[图片]"}]
        )

        self.assertTrue(await card._event_reply_contains_link_payload(object()))
        self.assertFalse(await image._event_reply_contains_link_payload(object()))

    def test_quoted_link_scene_cannot_trigger_question_wakeup(self) -> None:
        wakeup = self.wakeup._evaluate_group_wakeup(
            {},
            scene={"talking_to": "group", "trigger": "normal", "quoted_link_payload": True},
            sender_id="10001",
            sender_name="群友",
            text="这个值得买吗？",
            group_id="20001",
        )

        self.assertEqual(wakeup, {})

    async def test_wakeup_message_does_not_compete_with_interjection(self) -> None:
        await self.interjection._maybe_group_interject(_WakeEvent(), {}, "有没有人知道这个怎么弄？")

    def test_media_placeholders_are_not_repeat_signatures(self) -> None:
        harness = _RepeatHarness()
        for placeholder in ("[图片]", "【图片】", "图片", "[语音]", "[视频]", "[文件]"):
            self.assertEqual("", harness._group_repeat_signature(placeholder))

    async def test_distinct_image_placeholders_do_not_trigger_repeat_follow(self) -> None:
        harness = _RepeatHarness()
        group = {}

        with patch("astrbot_plugin_private_companion.group_observation.random.random", return_value=0.5):
            for _ in range(3):
                await harness._maybe_group_interject(
                    _RepeatEvent(),
                    group,
                    "[图片]",
                    allow_interjection=False,
                )

        self.assertEqual([], harness.replies)
        self.assertEqual({}, group.get("repeat_follow_state", {}))

    async def test_repeat_still_works_when_general_interjection_is_disabled(self) -> None:
        harness = _RepeatHarness()
        group = {}

        with patch("astrbot_plugin_private_companion.group_observation.random.random", return_value=0.5):
            for _ in range(3):
                await harness._maybe_group_interject(
                    _RepeatEvent(),
                    group,
                    "复读测试",
                    allow_interjection=False,
                )

        self.assertEqual(harness.replies, [("复读测试", "")])
        self.assertEqual(group["repeat_follow_state"]["count"], 3)
        self.assertTrue(group["repeat_follow_state"]["acted"])

    async def test_repeat_uses_structured_scene_when_adapter_marks_group_message_as_wake(self) -> None:
        harness = _RepeatHarness()
        group = {}
        events: list[_RepeatEvent] = []

        with patch("astrbot_plugin_private_companion.group_observation.random.random", return_value=0.5):
            for _ in range(3):
                event = _RepeatEvent(wake=True)
                events.append(event)
                await harness._maybe_group_interject(
                    event,
                    group,
                    "复读测试",
                    allow_interjection=False,
                    repeat_scene={"talking_to": "group", "trigger": "group_message"},
                )

        self.assertEqual(harness.replies, [("复读测试", "")])
        self.assertEqual(group["repeat_follow_state"]["count"], 3)
        self.assertTrue(events[-1].stopped)

    async def test_repeat_message_is_counted_only_once_across_early_and_late_paths(self) -> None:
        harness = _RepeatHarness()
        group = {}

        with patch("astrbot_plugin_private_companion.group_observation.random.random", return_value=0.5):
            for _ in range(3):
                event = _RepeatEvent()
                await harness._maybe_group_interject(
                    event,
                    group,
                    "复读测试",
                    allow_interjection=False,
                    repeat_scene={"talking_to": "group"},
                )
                await harness._maybe_group_interject(
                    event,
                    group,
                    "复读测试",
                    allow_interjection=False,
                    repeat_scene={"talking_to": "group"},
                )

        self.assertEqual(harness.replies, [("复读测试", "")])
        self.assertEqual(group["repeat_follow_state"]["count"], 3)

    async def test_repeat_interrupt_path_is_delivered(self) -> None:
        harness = _RepeatHarness()
        harness.group_repeat_follow_probability = 0.0
        harness.group_repeat_interrupt_probability = 1.0
        group = {}

        with patch("astrbot_plugin_private_companion.group_observation.random.random", return_value=0.5):
            for _ in range(3):
                await harness._maybe_group_interject(
                    _RepeatEvent(),
                    group,
                    "复读测试",
                    allow_interjection=False,
                    repeat_scene={"talking_to": "group"},
                )

        self.assertEqual(harness.replies, [("禁止复读", "")])
        self.assertEqual(group["last_bot_interjection"]["reason"], "群聊复读打断")


if __name__ == "__main__":
    unittest.main()
