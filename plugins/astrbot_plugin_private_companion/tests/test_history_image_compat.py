# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest

from tool_history_sanitizer import sanitize_history_image_blocks


ROOT = Path(__file__).resolve().parents[1]


class HistoricalImageSanitizerTests(unittest.TestCase):
    def test_image_blocks_are_replaced_without_removing_message_text(self) -> None:
        contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看这张"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ]

        cleaned, stats = sanitize_history_image_blocks(contexts)

        self.assertEqual(1, stats["image_blocks_replaced"])
        self.assertEqual("看看这张", cleaned[0]["content"][0]["text"])
        self.assertEqual({"type": "text", "text": "[历史图片]"}, cleaned[0]["content"][1])
        self.assertIn("image_url", contexts[0]["content"][1])

    def test_ordinary_text_history_is_a_noop(self) -> None:
        contexts = [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]

        cleaned, stats = sanitize_history_image_blocks(contexts)

        self.assertIs(contexts, cleaned)
        self.assertEqual(0, stats["changed"])


def _load_hook() -> Any:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PrivateCompanionPlugin")
    target = next(
        node
        for node in owner.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "sanitize_historical_image_blocks_before_provider"
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "AstrMessageEvent": Any,
        "ProviderRequest": Any,
        "_multi_persona_event_context": lambda value: value,
        "filter": SimpleNamespace(on_llm_request=lambda **_kwargs: lambda value: value),
        "sanitize_history_image_blocks": sanitize_history_image_blocks,
        "_single_line": lambda value, limit: str(value or "")[:limit],
        "logger": SimpleNamespace(info=lambda *_args, **_kwargs: None),
    }
    module = ast.Module(body=[copy.deepcopy(target)], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)
    return namespace[target.name]


class HistoricalImageHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_hook_preserves_current_turn_images(self) -> None:
        host = SimpleNamespace(enabled=True)
        request = SimpleNamespace(
            contexts=[
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "old.png"}}],
                }
            ],
            image_urls=["current.png"],
        )
        event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")

        await _load_hook()(host, event, request)

        self.assertEqual(["current.png"], request.image_urls)
        self.assertEqual([{"type": "text", "text": "[历史图片]"}], request.contexts[0]["content"])


if __name__ == "__main__":
    unittest.main()
