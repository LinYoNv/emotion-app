# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_private_companion.private_image import PrivateImageMixin


class _ContextImageHarness(PrivateImageMixin):
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        max_items: int = 12,
        caption_results: dict[tuple[str, ...], str] | None = None,
    ) -> None:
        self.enable_context_image_captioning = True
        self.context_image_caption_max_items = max_items
        self.rows = list(rows or [])
        self.caption_calls: list[tuple[str, ...]] = []
        self.caption_results = dict(caption_results or {})

    @staticmethod
    def _private_image_enhancement_enabled() -> bool:
        return True

    @staticmethod
    def _recall_image_items_from_snapshot(row: dict[str, Any]) -> list[dict[str, str]]:
        items = row.get("image_items")
        return list(items) if isinstance(items, list) else []

    def _context_image_recall_rows_for_event(self, _event: Any) -> list[dict[str, Any]]:
        return list(self.rows)

    async def _caption_context_image_sources(
        self,
        sources: list[str],
        *,
        umo: str = "",
    ) -> str:
        del umo
        cache_key = tuple(sources)
        self.caption_calls.append(cache_key)
        return self.caption_results.get(cache_key, "")


def _row(index: int, source: str) -> dict[str, Any]:
    return {
        "message_id": f"message-{index}",
        "text": f"第 {index} 张 [图片]",
        "image_items": [{"source": source, "tier": "url"}],
        "images": [source],
    }


def _contexts(count: int) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": f"第 {index} 张 [图片]"}
        for index in range(1, count + 1)
    ]


class ContextImageSourceRegressionTests(unittest.TestCase):
    def test_filtered_structured_platform_file_does_not_flow_back_from_legacy_images(self) -> None:
        source = "5A9F2D800733CFC36AC147C5AC471FDB.jpg"
        row = {
            "image_items": [{"source": source, "tier": "platform_file"}],
            "images": [source],
        }
        harness = _ContextImageHarness()

        self.assertEqual([], harness._context_image_sources_from_recall_row(row))

    def test_structured_local_and_url_sources_remain_eligible(self) -> None:
        row = {
            "image_items": [
                {"source": "platform-file-id.jpg", "tier": "platform_file"},
                {"source": "C:/AstrBot/cache/image.png", "tier": "local"},
                {"source": "https://example.invalid/image.png", "tier": "url"},
            ],
            "images": ["platform-file-id.jpg", "legacy-alias.jpg"],
        }
        harness = _ContextImageHarness()

        self.assertEqual(
            ["C:/AstrBot/cache/image.png", "https://example.invalid/image.png"],
            harness._context_image_sources_from_recall_row(row),
        )

    def test_legacy_images_keep_url_but_drop_nonexistent_bare_file_id(self) -> None:
        image_url = "https://example.invalid/legacy.png"
        with TemporaryDirectory() as temp_dir:
            local_image = Path(temp_dir) / "cached.jpg"
            local_image.write_bytes(b"cached image fixture")
            row = {
                "images": [
                    image_url,
                    str(local_image),
                    "NONEXISTENT_PLATFORM_FILE_7E12CE450A5445A1.jpg",
                ]
            }
            harness = _ContextImageHarness()

            self.assertEqual(
                [image_url, str(local_image)],
                harness._context_image_sources_from_recall_row(row),
            )


class ContextImageAttemptRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_image_segment_drops_bare_file_alias_when_url_is_available(self) -> None:
        image_url = "https://example.invalid/current.png"
        harness = _ContextImageHarness(max_items=2)
        request = SimpleNamespace(
            contexts=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看看这张 [图片]"},
                        {
                            "type": "image",
                            "data": {
                                "url": image_url,
                                "file": "5A9F2D800733CFC36AC147C5AC471FDB.jpg",
                            },
                        },
                    ],
                }
            ]
        )
        event = SimpleNamespace(unified_msg_origin="default:GroupMessage:10001")

        await harness._enrich_request_context_image_placeholders(event, request)

        self.assertEqual([(image_url,)], harness.caption_calls)

    async def test_max_items_caps_caption_attempts_even_when_every_caption_fails(self) -> None:
        rows = [_row(index, f"https://example.invalid/image-{index}.png") for index in range(1, 5)]
        harness = _ContextImageHarness(rows=rows, max_items=2)
        request = SimpleNamespace(contexts=_contexts(4))
        event = SimpleNamespace(unified_msg_origin="default:GroupMessage:10001")

        result = await harness._enrich_request_context_image_placeholders(event, request)

        self.assertEqual(
            [
                ("https://example.invalid/image-1.png",),
                ("https://example.invalid/image-2.png",),
            ],
            harness.caption_calls,
        )
        self.assertEqual(0, result["replaced"])

    async def test_same_failed_source_is_captioned_only_once_per_request(self) -> None:
        shared_source = "https://example.invalid/shared.png"
        rows = [_row(index, shared_source) for index in range(1, 5)]
        harness = _ContextImageHarness(rows=rows, max_items=12)
        request = SimpleNamespace(contexts=_contexts(4))
        event = SimpleNamespace(unified_msg_origin="default:GroupMessage:10001")

        result = await harness._enrich_request_context_image_placeholders(event, request)

        self.assertEqual([(shared_source,)], harness.caption_calls)
        self.assertEqual(0, result["replaced"])

    async def test_failed_recall_row_is_not_reused_by_the_next_placeholder(self) -> None:
        first_source = "https://example.invalid/first.png"
        second_source = "https://example.invalid/second.png"
        rows = [_row(1, first_source), _row(2, second_source)]
        for row in rows:
            row["text"] = "历史图片 [图片]"
        harness = _ContextImageHarness(
            rows=rows,
            max_items=4,
            caption_results={(second_source,): "第二张图片识别成功"},
        )
        request = SimpleNamespace(
            contexts=[
                {"role": "user", "content": "看看 [图片]"},
                {"role": "user", "content": "看看 [图片]"},
            ]
        )
        event = SimpleNamespace(unified_msg_origin="default:GroupMessage:10001")

        result = await harness._enrich_request_context_image_placeholders(event, request)

        self.assertEqual([(first_source,), (second_source,)], harness.caption_calls)
        self.assertEqual(1, result["replaced"])
        self.assertEqual("看看 [图片]", request.contexts[0]["content"])
        self.assertIn("第二张图片识别成功", request.contexts[1]["content"])


if __name__ == "__main__":
    unittest.main()
