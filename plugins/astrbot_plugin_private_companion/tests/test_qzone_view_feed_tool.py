# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from astrbot_plugin_private_companion.llm_tool_actions import LlmToolActionsMixin
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


BOT_UIN = "10000"
USER_UIN = "995051631"


class _Event:
    def __init__(self, text: str) -> None:
        self.message_str = text

    @staticmethod
    def get_sender_id() -> str:
        return USER_UIN


class _QzoneViewHarness(LlmToolActionsMixin):
    enabled = True
    enable_qzone_integration = True

    def __init__(self, posts: list[SimpleNamespace]) -> None:
        self.posts = posts
        self.query_calls: list[dict[str, object]] = []

    @staticmethod
    def _proactive_only_blocks_passive_event(_event, _surface: str) -> bool:
        return False

    @staticmethod
    def _qzone_available(_event=None) -> bool:
        return True

    @staticmethod
    async def _qzone_get_cookies(_event=None) -> str:
        return "uin=o10000; p_skey=test"

    @staticmethod
    def _qzone_context_from_cookies(_cookie_header: str) -> dict[str, object]:
        return {"uin": 10000, "gtk": "1"}

    async def _qzone_query_feeds(self, _event=None, **kwargs):
        self.query_calls.append(dict(kwargs))
        return list(self.posts)

    @staticmethod
    def _qzone_post_time_text(value: object) -> str:
        timestamp = float(value or 0)
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp > 0 else ""

    @staticmethod
    def _environment_now() -> datetime:
        return datetime(2026, 8, 4, 21, 0)

    @staticmethod
    def _environment_fromtimestamp(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp)

    @staticmethod
    def _qzone_post_value(post, key: str, default=""):
        return getattr(post, key, default)


class _ExpiredQzoneViewHarness(_QzoneViewHarness):
    @staticmethod
    async def _qzone_get_cookies(_event=None) -> str:
        raise RuntimeError("QQ 空间登录态已失效，需要重新绑定 Cookie")


class _MissingSecretQzoneViewHarness(_QzoneViewHarness):
    @staticmethod
    async def _qzone_get_cookies(_event=None) -> str:
        raise RuntimeError("Cookie 中缺少 p_skey/skey，无法计算 g_tk")


def _comment(uin: str, name: str, text: str, hour: int, minute: int) -> SimpleNamespace:
    return SimpleNamespace(
        comment_id=f"comment-{uin}-{hour}-{minute}",
        uin=uin,
        name=name,
        content=text,
        create_time=datetime(2026, 8, 4, hour, minute).timestamp(),
    )


def _post(
    uin: str,
    name: str,
    text: str,
    hour: int,
    minute: int,
    *,
    comments: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    timestamp = datetime(2026, 8, 4, hour, minute).timestamp()
    return SimpleNamespace(
        tid=f"post-{uin}-{hour}-{minute}",
        fid=f"post-{uin}-{hour}-{minute}",
        uin=uin,
        name=name,
        text=text,
        rt_con="",
        images=[],
        comments=list(comments or []),
        create_time=timestamp,
        abstime=int(timestamp),
    )


class QzoneViewFeedToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_self_time_query_selects_nearest_post_and_returns_comments(self) -> None:
        posts = [
            _post(BOT_UIN, "狐言", "晚上八点的最新动态", 20, 5),
            _post(
                BOT_UIN,
                "狐言",
                "下午六点多的目标动态",
                18,
                24,
                comments=[_comment(USER_UIN, "琳沐", "我看到了", 18, 30)],
            ),
        ]
        harness = _QzoneViewHarness(posts)

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你今天下午6点多发的QQ动态")
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("bot_self", payload["target_scope"])
        self.assertTrue(payload["target_verified"])
        self.assertEqual("下午六点多的目标动态", payload["text"])
        self.assertEqual("2026-08-04 18:24", payload["published_at"])
        self.assertTrue(payload["comments_loaded"])
        self.assertTrue(payload["current_user_commented"])
        self.assertEqual("我看到了", payload["comments"][0]["text"])
        self.assertEqual(BOT_UIN, harness.query_calls[0]["target_id"])
        self.assertEqual(30, harness.query_calls[0]["num"])

    async def test_user_owned_post_query_checks_for_bot_comment(self) -> None:
        harness = _QzoneViewHarness(
            [
                _post(
                    USER_UIN,
                    "琳沐",
                    "用户自己的说说",
                    18,
                    10,
                    comments=[_comment(BOT_UIN, "狐言", "我回复过啦", 18, 12)],
                )
            ]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("我自己的说说上你有没有回复？"),
                selector="最新",
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("current_user", payload["target_scope"])
        self.assertTrue(payload["bot_commented"])
        self.assertEqual(USER_UIN, harness.query_calls[0]["target_id"])

    async def test_unknown_and_time_alias_arguments_no_longer_raise(self) -> None:
        harness = _QzoneViewHarness(
            [_post(BOT_UIN, "狐言", "时间别名命中", 18, 20)]
        )

        payload = json.loads(
            await PrivateCompanionPlugin.pc_qzone_view_feed(
                harness,
                _Event("看看你发的动态"),
                target="self",
                time="今天18:20",
                detail=True,
                future_provider_argument="ignored safely",
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("时间别名命中", payload["text"])
        signature = inspect.signature(PrivateCompanionPlugin.pc_qzone_view_feed)
        self.assertEqual(inspect.Parameter.VAR_KEYWORD, signature.parameters["kwargs"].kind)

    async def test_target_alias_resolves_without_clear_pronoun_in_message(self) -> None:
        harness = _QzoneViewHarness(
            [_post(BOT_UIN, "狐言", "别名命中的动态", 18, 20)]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("帮我看看空间动态"),
                target="self",
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("bot_self", payload["target_scope"])
        self.assertEqual(BOT_UIN, harness.query_calls[0]["target_id"])

    async def test_message_ownership_overrides_conflicting_generated_arguments(self) -> None:
        harness = _QzoneViewHarness(
            [_post(BOT_UIN, "狐言", "Bot 自己下午发的动态", 18, 20)]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你今天下午6点多发的QQ动态"),
                user_id=USER_UIN,
                target_scope="current_user",
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("bot_self", payload["target_scope"])
        self.assertEqual(BOT_UIN, harness.query_calls[0]["target_id"])

    async def test_hour_more_hint_does_not_match_a_later_evening_post(self) -> None:
        harness = _QzoneViewHarness(
            [_post(BOT_UIN, "狐言", "晚上八点的动态", 20, 5)]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你今天下午6点多发的QQ动态")
            )
        )

        self.assertEqual("not_found_time", payload["status"])
        self.assertTrue(payload["must_not_claim_viewed"])

    async def test_chinese_hour_more_hint_selects_only_that_hour(self) -> None:
        harness = _QzoneViewHarness(
            [
                _post(BOT_UIN, "狐言", "晚上八点的最新动态", 20, 5),
                _post(BOT_UIN, "狐言", "下午六点多的目标动态", 18, 24),
            ]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你下午六点多发的QQ动态")
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("下午六点多的目标动态", payload["text"])
        self.assertEqual("2026-08-04 18:24", payload["published_at"])

    async def test_chinese_half_hour_hint_uses_thirty_minutes(self) -> None:
        harness = _QzoneViewHarness(
            [
                _post(BOT_UIN, "狐言", "晚上八点的最新动态", 20, 5),
                _post(BOT_UIN, "狐言", "六点刚过", 18, 2),
                _post(BOT_UIN, "狐言", "六点半的目标动态", 18, 31),
            ]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你今天下午六点半发的QQ动态")
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("六点半的目标动态", payload["text"])
        self.assertEqual("2026-08-04 18:31", payload["published_at"])

    async def test_period_only_hint_filters_out_later_evening_posts(self) -> None:
        harness = _QzoneViewHarness(
            [
                _post(BOT_UIN, "狐言", "晚上八点的最新动态", 20, 5),
                _post(BOT_UIN, "狐言", "下午五点的目标动态", 17, 24),
            ]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你今天下午发的QQ动态")
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("下午五点的目标动态", payload["text"])

    async def test_malformed_explicit_time_never_falls_back_to_latest(self) -> None:
        for message in (
            "看看你今天下午二十五点发的QQ动态",
            "看看你今天25:00发的QQ动态",
            "看看你今天下午25点发的QQ动态",
            "看看你今天18:发的QQ动态",
            "看看你今天下午六点一半发的QQ动态",
            "看看你2026年13月40日发的QQ动态",
        ):
            with self.subTest(message=message):
                harness = _QzoneViewHarness(
                    [_post(BOT_UIN, "狐言", "不应被误取的最新动态", 20, 5)]
                )
                payload = json.loads(
                    await harness._pc_qzone_view_feed_impl(_Event(message))
                )

                self.assertEqual("invalid_time_hint", payload["status"])
                self.assertFalse(payload["success"])
                self.assertFalse(payload["target_verified"])
                self.assertFalse(payload["should_retry"])
                self.assertEqual([], harness.query_calls)

    async def test_tomorrow_evening_hint_uses_tomorrow_evening_window(self) -> None:
        today_post = _post(BOT_UIN, "狐言", "今天晚上的动态", 21, 10)
        tomorrow_post = _post(BOT_UIN, "狐言", "明晚的目标动态", 20, 10)
        tomorrow_timestamp = datetime(2026, 8, 5, 20, 10).timestamp()
        tomorrow_post.create_time = tomorrow_timestamp
        tomorrow_post.abstime = int(tomorrow_timestamp)
        harness = _QzoneViewHarness([today_post, tomorrow_post])

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你明晚发的QQ动态")
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual("明晚的目标动态", payload["text"])
        self.assertEqual("2026-08-05 20:10", payload["published_at"])

    async def test_unrecognized_explicit_time_hint_does_not_query_latest(self) -> None:
        harness = _QzoneViewHarness(
            [_post(BOT_UIN, "狐言", "不应被误取的最新动态", 20, 5)]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你发的QQ动态"),
                time_hint="某个说不清的时间",
            )
        )

        self.assertEqual("invalid_time_hint", payload["status"])
        self.assertEqual("某个说不清的时间", payload["requested_time"])
        self.assertEqual([], harness.query_calls)

    async def test_author_mismatch_is_not_reported_as_success(self) -> None:
        harness = _QzoneViewHarness(
            [_post(USER_UIN, "琳沐", "不属于 Bot 的动态", 18, 20)]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你今天下午6点多发的QQ动态")
            )
        )

        self.assertEqual("target_mismatch", payload["status"])
        self.assertFalse(payload["success"])
        self.assertTrue(payload["must_not_claim_viewed"])
        self.assertNotIn("text", payload)

    async def test_missing_author_uin_is_not_reported_as_verified(self) -> None:
        harness = _QzoneViewHarness(
            [_post("", "未知作者", "无法确认归属的动态", 18, 20)]
        )

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你今天下午6点多发的QQ动态")
            )
        )

        self.assertEqual("target_unverified", payload["status"])
        self.assertFalse(payload["success"])
        self.assertTrue(payload["must_not_claim_viewed"])
        self.assertNotIn("text", payload)

    async def test_incomplete_comment_payload_does_not_claim_no_reply(self) -> None:
        post = _post(BOT_UIN, "狐言", "评论列表未完整返回", 18, 20)
        post.raw = {"cmtnum": 2}
        harness = _QzoneViewHarness([post])

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你的最新动态里有没有我的回复"),
                target_scope="self",
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertFalse(payload["comments_complete"])
        self.assertIsNone(payload["current_user_commented"])
        self.assertIsNone(payload["bot_commented"])

    async def test_more_than_thirty_loaded_comments_are_reported_as_truncated(self) -> None:
        comments = [
            _comment(str(20000 + index), f"访客{index}", f"评论{index}", 18, 20)
            for index in range(30)
        ]
        comments.append(_comment(USER_UIN, "琳沐", "第31条评论", 18, 21))
        post = _post(
            BOT_UIN,
            "狐言",
            "评论列表超过工具输出上限",
            18,
            20,
            comments=comments,
        )
        harness = _QzoneViewHarness([post])

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你的最新动态里有没有我的回复"),
                target_scope="self",
            )
        )

        self.assertEqual("success", payload["status"])
        self.assertEqual(31, payload["reported_comment_count"])
        self.assertEqual(30, payload["comment_count"])
        self.assertEqual(30, len(payload["comments"]))
        self.assertFalse(payload["comments_complete"])
        self.assertIsNone(payload["current_user_commented"])

    async def test_expired_cookie_returns_non_retrying_auth_result(self) -> None:
        harness = _ExpiredQzoneViewHarness([])

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你今天下午6点多发的QQ动态")
            )
        )

        self.assertEqual("auth_required", payload["status"])
        self.assertFalse(payload["success"])
        self.assertFalse(payload["should_retry"])
        self.assertTrue(payload["must_not_claim_viewed"])
        self.assertIn("不要重复调用", payload["final_response_instruction"])

    async def test_missing_cookie_secret_is_also_an_auth_result(self) -> None:
        harness = _MissingSecretQzoneViewHarness([])

        payload = json.loads(
            await harness._pc_qzone_view_feed_impl(
                _Event("看看你发的QQ动态")
            )
        )

        self.assertEqual("auth_required", payload["status"])
        self.assertFalse(payload["should_retry"])


if __name__ == "__main__":
    unittest.main()
