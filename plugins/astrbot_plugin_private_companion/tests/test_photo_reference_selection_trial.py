from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "astrbot_plugin_private_companion"
if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)

from astrbot_plugin_private_companion.photo_reference_selection import (
    SelectionResult,
    normalize_photo_selection_request,
    run_photo_selection_trial,
    select_photo_reference,
)


class PhotoReferenceSelectionTrialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.candidates = [
            {
                "id": "sleep",
                "reference_roles": ["identity", "outfit", "scene"],
                "outfit_category": "sleepwear",
                "scene_categories": ["home", "bedroom"],
                "time_categories": ["night", "bedtime"],
                "selection_eligibility": "matching_only",
            },
            {
                "id": "school",
                "reference_roles": ["identity", "outfit"],
                "outfit_category": "school_uniform",
                "scene_categories": ["school"],
                "selection_eligibility": "disabled",
            },
        ]

    def test_selection_matches_and_excludes_candidates(self) -> None:
        result = select_photo_reference(
            {"request_text": "晚上了，在卧室穿着睡衣给我拍一张吧"}, self.candidates
        )
        self.assertEqual(result.selected["id"], "sleep")
        self.assertEqual(result.selection_reason, "best_match")
        self.assertEqual(result.candidates[0].candidate_id, "sleep")
        self.assertIn("disabled", result.candidates[1].excluded)

    def test_identity_only_fallback_is_distinct_from_any_fallback(self) -> None:
        result = select_photo_reference(
            {"request_text": "在学校拍一张照片"},
            [
                {
                    "id": "identity-only",
                    "reference_roles": ["identity", "outfit"],
                    "scene_categories": ["home"],
                    "selection_eligibility": "fallback_identity_only",
                },
                {
                    "id": "any-fallback",
                    "reference_roles": ["identity", "outfit"],
                    "scene_categories": ["home"],
                    "selection_eligibility": "fallback_allowed",
                },
            ],
        )
        self.assertEqual(result.selected["id"], "any-fallback")
        excluded = {item.candidate_id: item.excluded for item in result.candidates}
        self.assertIn("identity_only_fallback", excluded["identity-only"])

    def test_selection_honors_scene_and_time_exclusions(self) -> None:
        result = select_photo_reference(
            {"request_text": "晚上在学校拍一张"},
            [
                {
                    "id": "blocked",
                    "reference_roles": ["identity"],
                    "excluded_scene_categories": ["school"],
                    "excluded_time_categories": ["night"],
                    "selection_eligibility": "fallback_allowed",
                }
            ],
        )
        self.assertIsNone(result.selected)
        self.assertEqual(set(result.candidates[0].excluded), {"scene", "time"})

    def test_negated_scene_is_not_treated_as_a_requested_scene(self) -> None:
        result = select_photo_reference(
            {"request_text": "不要在学校，在卧室拍一张"},
            [
                {
                    "id": "bedroom",
                    "reference_roles": ["identity", "scene"],
                    "scene_categories": ["bedroom"],
                    "excluded_scene_categories": ["school"],
                    "selection_eligibility": "matching_only",
                }
            ],
        )
        self.assertEqual(result.selected["id"], "bedroom")
        self.assertNotIn("scene", result.candidates[0].excluded)

    def test_matching_only_outfit_requires_an_outfit_match(self) -> None:
        result = select_photo_reference(
            {"request_text": "穿校服拍一张照片"},
            [
                {
                    "id": "sleepwear-only",
                    "reference_roles": ["identity", "outfit"],
                    "outfit_category": "sleepwear",
                    "selection_eligibility": "matching_only",
                }
            ],
        )
        self.assertIsNone(result.selected)
        self.assertIn("matching_only", result.candidates[0].excluded)

    async def test_trial_without_runner_is_explicit_and_side_effect_free(self) -> None:
        report = await run_photo_selection_trial(
            {"request_text": "晚上了，在卧室穿着睡衣给我拍一张吧"},
            candidates=self.candidates,
            runs=3,
        )
        self.assertEqual(report.tool_status, "no_tool_call")
        self.assertEqual(report.error_stage, "tool_decision")
        self.assertFalse(report.tool_called)
        self.assertIsNone(report.selection)

    async def test_trial_selection_uses_captured_tool_arguments(self) -> None:
        async def runner(_text: str, _request: dict) -> dict:
            return {
                "tool_name": "pc_generate_photo",
                "arguments": {
                    "kind": "selfie",
                    "prompt": "晚上在卧室穿睡衣拍照",
                    "scene_preset": "睡前",
                },
            }

        report = await run_photo_selection_trial(
            {"request_text": "帮我拍一张", "expected_reference_id": "sleep"},
            candidates=self.candidates,
            tool_runner=runner,
        )
        self.assertEqual(report.selection.selected["id"], "sleep")
        self.assertEqual(report.normalized_request["kind"], "selfie")
        self.assertEqual(report.normalized_request["outfit_category"], "sleepwear")
        self.assertEqual(report.rule_fallback["selected_id"], "sleep")
        self.assertTrue(report.rule_fallback["used"])
        self.assertTrue(report.expected_match)

    def test_normalized_request_keeps_kind_prompt_and_scene_preset(self) -> None:
        normalized = normalize_photo_selection_request(
            "拍一张",
            {"kind": "sticker", "prompt": "晚上穿睡衣", "scene_preset": "卧室睡前"},
        )
        self.assertEqual(normalized["kind"], "sticker")
        self.assertEqual(normalized["prompt"], "晚上穿睡衣")
        self.assertEqual(normalized["scene_preset"], "卧室睡前")
        self.assertEqual(normalized["outfit_category"], "sleepwear")
        self.assertIn("bedroom", normalized["scene_categories"])
        self.assertIn("night", normalized["time_categories"])

    def test_candidate_ids_and_non_finite_priorities_are_normalized(self) -> None:
        result = select_photo_reference(
            {"request_text": "自然自拍"},
            [
                {"id": "duplicate", "reference_roles": ["identity"], "priority": "NaN"},
                {"id": "duplicate", "reference_roles": ["identity"], "priority": "Infinity"},
                {"reference_roles": ["identity"], "priority": object()},
            ],
        )

        self.assertEqual(
            [item.candidate_id for item in result.candidates],
            ["candidate-3", "duplicate", "duplicate#2"],
        )
        self.assertTrue(all(item.score == item.score for item in result.candidates))

    async def test_explicit_reference_bypasses_catalog_selection(self) -> None:
        selection_calls = 0

        async def tool_runner(_text: str, _request: dict) -> dict:
            return {
                "tool_name": "pc_generate_photo",
                "arguments": {
                    "kind": "edit",
                    "prompt": "改成夜景",
                    "reference_image_path": "C:/images/source.png",
                },
            }

        async def selection_runner(*_args) -> SelectionResult:
            nonlocal selection_calls
            selection_calls += 1
            raise AssertionError("显式参考图不应再进入目录选择")

        report = await run_photo_selection_trial(
            {"request_text": "把这张图改成夜景"},
            candidates=self.candidates,
            tool_runner=tool_runner,
            selection_runner=selection_runner,
        )

        self.assertEqual(selection_calls, 0)
        self.assertEqual(report.selection.selection_source, "explicit_reference")
        self.assertEqual(report.selection.selected["id"], "explicit_reference")
        self.assertEqual(report.normalized_request["explicit_reference_image_path"], "C:/images/source.png")

    async def test_trial_distinguishes_model_selection_from_rule_fallback(self) -> None:
        async def tool_runner(_text: str, _request: dict) -> dict:
            return {
                "tool_name": "pc_generate_photo",
                "arguments": {"kind": "selfie", "prompt": "卧室自拍"},
            }

        async def selection_runner(_request, candidates, rule_selection) -> SelectionResult:
            return SelectionResult(
                selected=candidates[0],
                candidates=rule_selection.candidates,
                selection_source="model",
                selection_reason="valid_candidate_number",
                fallback_id=rule_selection.fallback_id,
                model_attempted=True,
                model_selected_id="sleep",
            )

        report = await run_photo_selection_trial(
            {"request_text": "卧室自拍", "expected_reference_id": "sleep"},
            candidates=self.candidates,
            tool_runner=tool_runner,
            selection_runner=selection_runner,
        )

        self.assertTrue(report.model_selection["attempted"])
        self.assertTrue(report.model_selection["used"])
        self.assertEqual(report.model_selection["selected_id"], "sleep")
        self.assertFalse(report.rule_fallback["used"])
        self.assertTrue(report.expected_match)

    async def test_trial_uses_current_context_when_tool_arguments_are_generic(self) -> None:
        async def runner(_text: str, _request: dict) -> dict:
            return {
                "tool_name": "pc_generate_photo",
                "arguments": {"kind": "selfie", "prompt": "拍一张自然自拍"},
            }

        report = await run_photo_selection_trial(
            {
                "request_text": "帮我拍一张",
                "ambient_context": "当前位置：卧室",
            },
            candidates=self.candidates,
            tool_runner=runner,
        )
        self.assertEqual(report.selection.selected["id"], "sleep")

    async def test_trial_captures_only_photo_tool_arguments(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def runner(text: str, request: dict) -> dict:
            calls.append((text, request))
            return {
                "tool_name": "pc_generate_photo",
                "arguments": {"kind": "selfie", "prompt": "captured"},
            }

        report = await run_photo_selection_trial(
            {"request_text": "拍一张自然自拍"},
            candidates=self.candidates,
            tool_runner=runner,
            runs=3,
        )
        self.assertEqual(report.tool_status, "captured")
        self.assertTrue(report.tool_called)
        self.assertEqual(report.tool_name, "pc_generate_photo")
        self.assertEqual(report.tool_arguments["kind"], "selfie")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0], "拍一张自然自拍")
        self.assertTrue(report.stability["stable"])

    async def test_three_runs_detect_different_tool_arguments(self) -> None:
        calls = 0

        async def runner(_text: str, _request: dict) -> dict:
            nonlocal calls
            calls += 1
            return {
                "tool_name": "pc_generate_photo",
                "arguments": {"kind": "selfie", "prompt": f"captured-{calls}"},
            }

        report = await run_photo_selection_trial(
            {"request_text": "拍一张自然自拍"},
            candidates=self.candidates,
            tool_runner=runner,
            runs=3,
        )
        self.assertEqual(calls, 3)
        self.assertFalse(report.stability["stable"])
        self.assertEqual(len(report.stability["tool_arguments"]), 3)

    async def test_three_runs_detect_different_selection_sources(self) -> None:
        calls = 0

        async def tool_runner(_text: str, _request: dict) -> dict:
            return {
                "tool_name": "pc_generate_photo",
                "arguments": {"kind": "selfie", "prompt": "卧室自拍"},
            }

        async def selection_runner(_request, _candidates, rule_selection) -> SelectionResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return rule_selection
            return SelectionResult(
                selected=rule_selection.selected,
                candidates=rule_selection.candidates,
                selection_source="model",
                selection_reason="valid_candidate_number",
                fallback_id=rule_selection.fallback_id,
                model_attempted=True,
                model_selected_id=rule_selection.fallback_id,
            )

        report = await run_photo_selection_trial(
            {"request_text": "卧室自拍"},
            candidates=self.candidates,
            tool_runner=tool_runner,
            selection_runner=selection_runner,
            runs=3,
        )

        self.assertFalse(report.stability["stable"])
        self.assertEqual(report.stability["selection_sources"], ["rule_fallback", "model", "model"])

    async def test_trial_preserves_model_errors_as_tool_decision_errors(self) -> None:
        async def runner(_text: str, _request: dict) -> dict:
            return {
                "tool_name": "",
                "arguments": {},
                "status": "model_error",
                "error": "provider failed",
            }

        report = await run_photo_selection_trial(
            {"request_text": "拍一张照片"},
            candidates=self.candidates,
            tool_runner=runner,
        )
        self.assertEqual(report.tool_status, "model_error")
        self.assertEqual(report.error_stage, "tool_decision")
        self.assertEqual(report.error, "provider failed")

    async def test_trial_requires_real_user_text(self) -> None:
        report = await run_photo_selection_trial({}, candidates=self.candidates)
        self.assertEqual(report.tool_status, "invalid_request")
        self.assertEqual(report.error_stage, "tool_decision")


if __name__ == "__main__":
    unittest.main()
