# -*- coding: utf-8 -*-
"""外部插件合成事件的分段补发必须走平台直发，不能落到基类空实现。

背景：AstrBot 基类 ``AstrMessageEvent.send()`` 是空实现，既不发送也不抛异常。
外部插件（例如屏幕伴侣）会自行构造基类合成事件来触发 ``OnDecoratingResultEvent``，
private_companion 随后在后台任务里对这类事件调用 ``event.send()`` 补发第 2 段及
之后的内容，结果被静默丢弃、日志却记为“已发送”。

本文件锁定修复后的行为：事件不具备投递能力时改走 ``_send_chain_components``。
"""
from __future__ import annotations

import asyncio
import unittest

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


def _chain(text: str):
    return [Plain(text)]


class _BaseEventLike:
    """模拟“未重写 send”的事件：send 就是基类空实现。"""

    unified_msg_origin = "default:FriendMessage:1461445049"
    send = AstrMessageEvent.send

    def get_sender_id(self) -> str:
        return "1461445049"

    platform_meta = type("M", (), {"name": "aiocqhttp"})()


class _PlatformEventLike(AstrMessageEvent):
    """模拟平台适配器子类：重写了 send，可以真正投递。"""

    unified_msg_origin = "default:FriendMessage:1461445049"

    def __init__(self) -> None:
        self.sent: list[object] = []

    def get_sender_id(self) -> str:
        return "1461445049"

    platform_meta = type("M", (), {"name": "aiocqhttp"})()

    def chain_result(self, chain):
        return ("chain", list(chain))

    async def send(self, message) -> None:
        self.sent.append(message)


class _RemainderHarness(PrivateCompanionPlugin):
    """最小替身：只提供补发链路真正用到的那几个钩子。"""

    def __init__(self) -> None:
        self.platform_calls: list[tuple] = []
        self._accepts = True
        self.enable_tts_enhancement = False

    async def _send_chain_components(self, umo, chain, **kwargs):
        self.platform_calls.append((umo, chain, kwargs))
        return self._accepts

    def _event_scope_key(self, _event) -> str:
        return "private:1461445049"

    def _segmented_remainder_lock(self, _scope):
        class _Lock:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_exc):
                return False

        return _Lock()

    async def _calc_segmented_proactive_interval(self, *_a, **_k) -> float:
        return 0.0

    def _segmented_remainder_context_drift_reason(self, *_a, **_k) -> str:
        return ""

    async def _should_cancel_reply_for_missing_or_recalled_trigger(self, _event) -> str:
        return ""

    def _sanitize_segmented_plain_text(self, _event, text) -> str:
        return str(text or "")

    def _strip_plaintext_tool_call_envelopes(self, text):
        return str(text or ""), []

    def _chain_text_for_forbidden_recall(self, _chain) -> str:
        return ""

    def _forbidden_recall_hit(self, _text) -> str:
        return ""

    def _segmented_chunk_log_text(self, chunk) -> str:
        return "".join(str(getattr(item, "text", "") or "") for item in chunk)


class SegmentedRemainderExternalProactiveTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # 能力判定本身
    # ------------------------------------------------------------------
    def test_base_class_event_cannot_deliver(self) -> None:
        """未重写 send 的事件必须被判定为不可直接投递。"""
        self.assertFalse(
            PrivateCompanionPlugin._event_can_deliver_directly(_BaseEventLike())
        )

    def test_platform_subclass_event_can_deliver(self) -> None:
        """重写了 send 的平台子类必须被判定为可直接投递。"""
        self.assertTrue(
            PrivateCompanionPlugin._event_can_deliver_directly(_PlatformEventLike())
        )

    def test_detection_failure_falls_back_to_original_behaviour(self) -> None:
        """判定抛异常时返回 True，回退原行为，不会更差。"""

        class _Weird:
            @property
            def __class__(self):  # pragma: no cover - 故意触发判定异常
                raise RuntimeError("boom")

        self.assertTrue(PrivateCompanionPlugin._event_can_deliver_directly(_Weird()))

    # ------------------------------------------------------------------
    # 合成事件：必须走平台直发
    # ------------------------------------------------------------------
    def test_remainder_uses_platform_route_for_synthetic_event(self) -> None:
        plugin = _RemainderHarness()
        path = asyncio.run(
            PrivateCompanionPlugin._send_segmented_remainder_chain(
                plugin, _BaseEventLike(), _chain("第二段")
            )
        )
        self.assertEqual("platform", path)
        self.assertEqual(1, len(plugin.platform_calls))
        umo, chain, kwargs = plugin.platform_calls[0]
        self.assertEqual("default:FriendMessage:1461445049", umo)
        self.assertEqual("第二段", chain[0].text)
        self.assertFalse(kwargs.get("apply_decorating_hooks"))

    def test_synthetic_remainder_rejects_platform_refusal(self) -> None:
        """平台未接受时必须抛错，不能静默当作成功。"""
        plugin = _RemainderHarness()
        plugin._accepts = False
        with self.assertRaises(RuntimeError):
            asyncio.run(
                PrivateCompanionPlugin._send_segmented_remainder_chain(
                    plugin, _BaseEventLike(), _chain("第二段")
                )
            )

    def test_chain_remainder_routes_synthetic_event_to_platform(self) -> None:
        """完整补发链路：合成事件下每一段都经平台直发。"""
        plugin = _RemainderHarness()
        asyncio.run(
            PrivateCompanionPlugin._send_segmented_llm_chain_remainder(
                plugin,
                _BaseEventLike(),
                [_chain("第二段。"), _chain("第三段。")],
                previous_segment="第一段。",
                source="decorating_result",
                started_at=1.0,
            )
        )
        self.assertEqual(2, len(plugin.platform_calls))
        self.assertEqual(
            ["第二段。", "第三段。"],
            [call[1][0].text for call in plugin.platform_calls],
        )

    # ------------------------------------------------------------------
    # 真实平台事件：必须保持原路径
    # ------------------------------------------------------------------
    def test_real_platform_event_keeps_event_route(self) -> None:
        plugin = _RemainderHarness()
        event = _PlatformEventLike()
        path = asyncio.run(
            PrivateCompanionPlugin._send_segmented_remainder_chain(
                plugin, event, _chain("第二段")
            )
        )
        self.assertEqual("event", path)
        self.assertEqual([], plugin.platform_calls, "真实平台事件不应改走平台直发")
        self.assertEqual(1, len(event.sent))

    def test_chain_remainder_keeps_event_route_for_platform_event(self) -> None:
        plugin = _RemainderHarness()
        event = _PlatformEventLike()
        asyncio.run(
            PrivateCompanionPlugin._send_segmented_llm_chain_remainder(
                plugin,
                event,
                [_chain("第二段。"), _chain("第三段。")],
                previous_segment="第一段。",
                source="decorating_result",
                started_at=1.0,
            )
        )
        self.assertEqual([], plugin.platform_calls)
        self.assertEqual(2, len(event.sent))

    # ------------------------------------------------------------------
    # 私有标志路径：行为不变
    # ------------------------------------------------------------------
    def test_external_proactive_flag_still_uses_platform(self) -> None:
        plugin = _RemainderHarness()
        event = _PlatformEventLike()
        event._private_companion_external_proactive_source = "proactive_chat"
        path = asyncio.run(
            PrivateCompanionPlugin._send_segmented_remainder_chain(
                plugin, event, _chain("第二段")
            )
        )
        self.assertEqual("platform", path)
        self.assertEqual([], event.sent)
        self.assertEqual(1, len(plugin.platform_calls))


if __name__ == "__main__":
    unittest.main()
