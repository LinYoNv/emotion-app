import unittest
from datetime import datetime
from types import SimpleNamespace

from astrbot.api.message_components import Plain as MessagePlain
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
from astrbot_plugin_private_companion.user_memory import UserMemoryMixin


class Reply:
    def __init__(self, message_id: str):
        self.id = message_id


class Plain:
    def __init__(self, text: str):
        self.text = text


class _Result:
    def __init__(self, chain):
        self.chain = list(chain)

    @staticmethod
    def is_llm_result():
        return True


class _PrivateEvent:
    unified_msg_origin = "default:FriendMessage:10001"

    def __init__(self, chain, *, message_id="current-message"):
        self.result = _Result(chain)
        self.message_obj = SimpleNamespace(message_id=message_id, raw_message={"message_id": message_id})

    @staticmethod
    def is_private_chat():
        return True

    def get_result(self):
        return self.result

    def set_result(self, result):
        self.result = result


class _TimeAnchorHarness(UserMemoryMixin):
    @staticmethod
    def _environment_now():
        return datetime(2026, 7, 14, 8, 43)


class _TopicHintHarness(UserMemoryMixin):
    enable_passive_topic_suppression = True
    passive_topic_memory_hours = 8

    @staticmethod
    def _format_timestamp_elapsed(_value):
        return "刚刚"


class PrivateReplyScopeAndTimeAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_passive_llm_reply_drops_unexpected_quote_component(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        event = _PrivateEvent([Reply("other-message"), Plain("接住当前私聊")])

        await plugin.strip_unexpected_private_passive_reply(event)

        self.assertEqual(len(event.result.chain), 1)
        self.assertEqual(event.result.chain[0].text, "接住当前私聊")

    async def test_private_passive_llm_reply_keeps_current_message_quote(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        event = _PrivateEvent([Reply("current-message"), Plain("正常引用当前消息")])

        await plugin.strip_unexpected_private_passive_reply(event)

        self.assertEqual(len(event.result.chain), 2)
        self.assertIsInstance(event.result.chain[0], Reply)

    async def test_proactive_framework_keeps_explicit_private_quote_component(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        event = _PrivateEvent([Reply("planned-trigger"), Plain("预约消息")])
        event.private_companion_proactive_framework = True

        await plugin.strip_unexpected_private_passive_reply(event)

        self.assertEqual(len(event.result.chain), 2)

    def test_morning_rejects_implicit_late_night_anchor(self):
        harness = _TimeAnchorHarness()

        self.assertTrue(harness._response_has_invalid_current_time_anchor("时间不早了，真的要歇息了吧？"))
        self.assertTrue(harness._response_has_invalid_current_time_anchor("都这么晚了，早点睡吧。"))
        self.assertFalse(harness._response_has_invalid_current_time_anchor("这一大早的，在想什么呢？"))

    def test_local_fallback_removes_invalid_morning_sleep_tail(self):
        harness = _TimeAnchorHarness()
        response = "啊，我是说刚才话题跳太快了。那测试用户～时间不早了，真的要歇息了吧？"

        cleaned = harness._fallback_temporal_or_continuity_confused_reply(
            "什么",
            response,
            flags=["invalid_current_time_anchor"],
            user={},
        )

        self.assertIn("话题跳太快", cleaned)
        self.assertNotIn("时间不早", cleaned)
        self.assertNotIn("歇息", cleaned)

    def test_local_fallback_removes_invalid_time_after_generic_address(self):
        harness = _TimeAnchorHarness()

        cleaned = harness._fallback_temporal_or_continuity_confused_reply(
            "什么",
            "我刚才把话题接偏了。小林，快十一点了，困不困？",
            flags=["invalid_current_time_anchor"],
            user={},
        )

        self.assertEqual("我刚才把话题接偏了", cleaned)

    def test_daytime_clock_mention_is_not_a_late_night_anchor(self):
        """白天的 11 点只是时间事实：行程、用药、约见都不该被判成深夜宣言。"""
        harness = _TimeAnchorHarness()

        for text in (
            "行程是 9 点出门、11 点到站，午饭在车上解决。",
            "把闹钟设在十一点半，别提前热。",
            "会议定在 11 点半开始。",
            "预约在 11 点前后，出门留够半小时。",
        ):
            self.assertFalse(harness._response_has_invalid_current_time_anchor(text), text)

    def test_local_fallback_keeps_daytime_clock_mention(self):
        """即使这一轮已被标记，本地兜底也不该删掉白天的钟点，更不能只删半截。"""
        harness = _TimeAnchorHarness()

        for text in (
            "行程是 9 点出门、11 点到站，午饭在车上解决。",
            "把闹钟设在十一点半，别提前热。",
            "预约在 11 点前后，出门留够半小时。",
        ):
            cleaned = harness._fallback_temporal_or_continuity_confused_reply(
                "什么",
                text,
                flags=["invalid_current_time_anchor"],
                user={},
            )
            self.assertEqual(text, cleaned)

    def test_late_clock_claim_with_sleep_cue_still_flagged(self):
        harness = _TimeAnchorHarness()

        for text in (
            "快十一点了，困不困？",
            "都十一点了，该睡了吧。",
            "十一点了，早点睡。",
        ):
            self.assertTrue(harness._response_has_invalid_current_time_anchor(text), text)

    def test_local_fallback_removes_complete_late_clock_claim(self):
        harness = _TimeAnchorHarness()

        cleaned = harness._fallback_temporal_or_continuity_confused_reply(
            "什么",
            "今晚的安排记下了。快十一点了，困不困？",
            flags=["invalid_current_time_anchor"],
            user={},
        )

        self.assertEqual("今晚的安排记下了", cleaned)

    def test_local_fallback_keeps_text_after_implicit_late_claim(self):
        """「时间不早了」后面没有睡意线索时，只删这句宣言，不吃掉后面那句话。"""
        harness = _TimeAnchorHarness()

        cleaned = harness._fallback_temporal_or_continuity_confused_reply(
            "什么",
            "时间不早了，先吃饭吧。",
            flags=["invalid_current_time_anchor"],
            user={},
        )

        self.assertEqual("先吃饭吧。", cleaned)

    def test_routine_check_boundary_requires_evidence_and_one_focus(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)

        boundary = plugin._format_private_routine_check_boundary("那……例行检查")

        self.assertIn("最多提出一个问题", boundary)
        self.assertIn("要和后面的承接正文自然写在同一句里", boundary)
        self.assertIn("不要假定用户正在服药", boundary)
        self.assertIn("今天想先检查哪一项", boundary)
        self.assertEqual("", plugin._format_private_routine_check_boundary("上次例行检查结果是什么"))

    def test_recent_topic_hint_does_not_expose_relative_time_openers(self):
        harness = _TopicHintHarness()
        hint = harness._format_recent_passive_topics_hint(
            {
                "recent_reply_topics": [
                    {
                        "ts": datetime.now().timestamp(),
                        "text": "刚写到你喂我喝水那段了",
                        "signature": "喝水",
                    },
                ]
            }
        )

        self.assertIn("已用主题词", hint)
        self.assertIn("喝水", hint)
        self.assertNotIn("刚写到你喂我喝水那段了", hint)
        self.assertNotIn("刚刚回复过", hint)
        self.assertNotIn("刚才回复过", hint)

    def test_passive_reply_policy_prioritizes_current_topic_continuity(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        policy = plugin._private_passive_state_reply_policy_prompt()

        self.assertIn("当前用户最后一条消息是本轮唯一的主线", policy)
        self.assertIn("明确语义连接", policy)
        self.assertIn("相对时间词只在用户明确提到时间", policy)

    def test_routine_check_segment_count_is_limited_to_two(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        chunks = [[Plain("先接住")], [Plain("第一问")], [Plain("第二问")], [Plain("第三问")]]

        limited = plugin._limit_private_routine_check_segments("嗯，那就晚间检查一下", chunks)

        self.assertEqual(2, len(limited))
        self.assertEqual(["第一问", "第二问", "第三问"], [part.text for part in limited[1]])
        self.assertEqual(chunks, plugin._limit_private_routine_check_segments("检查一下这个报错", chunks))

    def test_routine_check_merges_interjection_and_address_lead(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        chunks = [[MessagePlain("唔，比折大人")], [MessagePlain("都凌晨三点半了还来例行检查呀。")]]

        limited = plugin._limit_private_routine_check_segments("例行检查", chunks)

        self.assertEqual(1, len(limited))
        self.assertEqual("唔，比折大人，都凌晨三点半了还来例行检查呀。", limited[0][0].text)

    def test_routine_check_keeps_normal_short_lead_separate(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        chunks = [[MessagePlain("先接住")], [MessagePlain("再问今天检查哪一项。")]]

        limited = plugin._limit_private_routine_check_segments("例行检查", chunks)

        self.assertEqual(chunks, limited)

if __name__ == "__main__":
    unittest.main()
