from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


try:
    from .package_bootstrap import bootstrap_package
except ImportError:
    from package_bootstrap import bootstrap_package


bootstrap_package()

from astrbot_plugin_memory_companion.core.service import MemoryCompanionService


class TokenUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(MemoryCompanionService)

    def test_failed_call_is_estimated_without_reported_tokens(self) -> None:
        usage = self.service._extract_token_usage(
            None,
            "一段中文输入",
            "",
        )

        self.assertEqual(0, usage["reported_tokens"])
        self.assertGreater(usage["estimated_tokens"], 0)
        self.assertEqual("estimated", usage["usage_source"])
        self.assertEqual(usage["estimated_tokens"], usage["total_tokens"])

    def test_provider_usage_is_not_marked_as_estimated(self) -> None:
        response = SimpleNamespace(usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})

        usage = self.service._extract_token_usage(response, "prompt", "completion")

        self.assertEqual(20, usage["reported_tokens"])
        self.assertEqual(0, usage["estimated_tokens"])
        self.assertEqual("provider", usage["usage_source"])

    def test_legacy_buckets_are_backfilled(self) -> None:
        payload = {
            "totals": {"calls": 2, "total_tokens": 30, "estimated_tokens": 10},
            "by_task": {"memory_summary": {"calls": 2, "total_tokens": 30, "estimated_tokens": 10}},
        }

        normalized = self.service._normalize_token_usage(payload)

        self.assertEqual(20, normalized["totals"]["reported_tokens"])
        self.assertEqual(20, normalized["by_task"]["memory_summary"]["reported_tokens"])
        self.assertEqual(2, normalized["totals"]["calls"])
        self.assertEqual(0, normalized["totals"]["estimated_calls"])

    def test_record_keeps_reported_and_estimated_counters_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.service._token_usage = {}
            self.service.data_dir = Path(temp)
            self.service.token_usage_path = Path(temp) / "usage.json"
            self.service._token_usage_last_save_at = 0.0

            self.service._record_token_usage(
                task="memory_summary",
                provider_id="test",
                prompt="失败请求",
                success=False,
                error="timeout",
            )
            self.service._record_token_usage(
                task="memory_summary",
                provider_id="test",
                prompt="prompt",
                completion="completion",
                resp=SimpleNamespace(usage={"total_tokens": 20}),
            )

            totals = self.service._token_usage["totals"]
            self.assertEqual(1, totals["errors"])
            self.assertEqual(20, totals["reported_tokens"])
            self.assertGreater(totals["estimated_tokens"], 0)
            self.assertEqual(
                totals["total_tokens"],
                totals["reported_tokens"] + totals["estimated_tokens"],
            )
            self.assertEqual(1, totals["estimated_calls"])


if __name__ == "__main__":
    unittest.main()
