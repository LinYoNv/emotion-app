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

from astrbot_plugin_private_companion.photo_reference_metadata import (
    build_reference_metadata_review_prompt,
    compile_reference_metadata,
    merge_reference_questionnaire_evidence,
    normalize_reviewed_reference_intent,
)


class PhotoReferenceMetadataTests(unittest.TestCase):
    def test_outfit_behaviors_compile_to_distinct_contracts(self) -> None:
        base = {"preserve": ["identity", "outfit"], "outfit_category": "sleepwear"}
        ignored = compile_reference_metadata({**base, "outfit_behavior": "ignore"})
        unlocked = compile_reference_metadata(
            {**base, "outfit_behavior": "reference_without_lock"}
        )
        locked = compile_reference_metadata(
            {**base, "outfit_behavior": "preserve_unless_explicit_change"}
        )
        self.assertEqual(ignored.metadata["outfit_category"], "")
        self.assertFalse(ignored.metadata["outfit_lock_default"])
        self.assertEqual(unlocked.metadata["outfit_category"], "sleepwear")
        self.assertFalse(unlocked.metadata["outfit_lock_default"])
        self.assertTrue(locked.metadata["outfit_lock_default"])

    def test_compile_reports_sources_differences_conflicts_and_trials(self) -> None:
        result = compile_reference_metadata(
            {
                "preserve": ["identity", "scene"],
                "prefer": {"scenes": ["bedroom"], "times": ["night"]},
                "avoid": {"scenes": ["bedroom"], "times": []},
                "preferred_preset": "missing",
            },
            ["home"],
            saved={"scene_categories": ["home"]},
        )
        payload = result.to_dict()
        self.assertTrue(payload["fields"])
        self.assertTrue(payload["differences"])
        self.assertTrue(payload["conflicts"])
        self.assertTrue(payload["recommended_trials"])
        self.assertEqual(result.metadata["metadata_source"], "guided_editor")

    def test_compile_does_not_mutate_saved_mapping(self) -> None:
        saved = {"reference_roles": ["identity"], "scene_categories": ["home"]}
        compile_reference_metadata({"preserve": ["scene"]}, saved=saved)
        self.assertEqual(saved, {"reference_roles": ["identity"], "scene_categories": ["home"]})

    def test_redundant_questions_merge_into_shared_responsibility_evidence(self) -> None:
        questionnaire = {
            "version": 2,
            "answers": [
                {
                    "id": "core_anchor",
                    "question": "这张图最不能丢的特点是什么？",
                    "selections": [
                        {"field": "core_anchor", "value": "identity", "label": "人物长相"},
                        {"field": "core_anchor", "value": "outfit", "label": "整套穿搭"},
                    ],
                },
                {
                    "id": "wardrobe_change",
                    "question": "换一身衣服后，这张图还适合用吗？",
                    "selections": [
                        {"field": "wardrobe_change", "value": "no_outfit_core", "label": "不适合，穿搭就是重点"},
                    ],
                },
                {
                    "id": "pose_change",
                    "question": "动作改变后，这张图还适合用吗？",
                    "selections": [
                        {"field": "pose_change", "value": "yes_identity", "label": "适合，主要看人物长相"},
                    ],
                },
                {
                    "id": "outfit_rule",
                    "question": "图中穿搭应该怎么处理？",
                    "selections": [
                        {"field": "outfit_behavior", "value": "preserve_unless_explicit_change", "label": "通常保持"},
                        {"field": "outfit_category", "value": "sleepwear", "label": "睡衣"},
                    ],
                },
            ],
        }

        merged = merge_reference_questionnaire_evidence(questionnaire)

        self.assertEqual(merged["preserve"][:2], ["identity", "outfit"])
        self.assertEqual(merged["outfit_category"], "sleepwear")
        evidence = merged["evidence"]
        self.assertGreaterEqual(len(evidence["identity"]), 2)
        self.assertGreaterEqual(len(evidence["outfit"]), 2)

    def test_llm_review_is_restricted_to_supported_metadata_values(self) -> None:
        fallback = {
            "preserve": ["identity"],
            "outfit_behavior": "reference_without_lock",
            "outfit_category": "sleepwear",
            "prefer": {"scenes": ["home"], "times": ["night"]},
            "avoid": {"scenes": [], "times": []},
            "selection_eligibility": "matching_only",
            "preferred_preset": "自拍",
        }
        reviewed = normalize_reviewed_reference_intent(
            {
                "intent": {
                    "preserve": ["identity", "invented_role"],
                    "outfit_behavior": "invalid_behavior",
                    "outfit_category": "invented_outfit",
                    "prefer": {"scenes": ["home", "moon"], "times": ["night", "century"]},
                    "avoid": {"scenes": ["school", "mars"], "times": []},
                    "selection_eligibility": "always",
                    "preferred_preset": "不存在",
                }
            },
            fallback,
            available_presets=["自拍", "室内"],
        )

        self.assertEqual(reviewed["preserve"], ["identity"])
        self.assertEqual(reviewed["outfit_behavior"], "reference_without_lock")
        self.assertEqual(reviewed["outfit_category"], "sleepwear")
        self.assertEqual(reviewed["prefer"], {"scenes": ["home"], "times": ["night"]})
        self.assertEqual(reviewed["avoid"], {"scenes": ["school"], "times": []})
        self.assertEqual(reviewed["selection_eligibility"], "matching_only")
        self.assertEqual(reviewed["preferred_preset"], "自拍")

    def test_llm_review_can_explicitly_clear_local_preferences(self) -> None:
        reviewed = normalize_reviewed_reference_intent(
            {
                "intent": {
                    "preserve": ["identity"],
                    "prefer": {"scenes": [], "times": []},
                    "avoid": {"scenes": [], "times": []},
                }
            },
            {
                "preserve": ["identity"],
                "prefer": {"scenes": ["home"], "times": ["night"]},
                "avoid": {"scenes": ["school"], "times": ["daytime"]},
            },
        )
        self.assertEqual(reviewed["prefer"], {"scenes": [], "times": []})
        self.assertEqual(reviewed["avoid"], {"scenes": [], "times": []})

    def test_llm_review_can_soften_local_outfit_preservation(self) -> None:
        reviewed = normalize_reviewed_reference_intent(
            {
                "intent": {
                    "preserve": ["identity", "outfit"],
                    "outfit_behavior": "reference_without_lock",
                }
            },
            {
                "preserve": ["identity", "outfit"],
                "outfit_behavior": "preserve_unless_explicit_change",
            },
        )

        self.assertEqual(reviewed["outfit_behavior"], "reference_without_lock")

    def test_compiled_field_sources_name_questions_and_selected_answers(self) -> None:
        result = compile_reference_metadata(
            {
                "preserve": ["identity"],
                "questionnaire": {
                    "version": 2,
                    "answers": [
                        {
                            "id": "core_anchor",
                            "question": "这张图最不能丢的特点是什么？",
                            "selections": [
                                {"field": "core_anchor", "value": "identity", "label": "人物长相"}
                            ],
                        },
                        {
                            "id": "fallback_policy",
                            "question": "没有完全匹配时怎么办？",
                            "selections": [
                                {"field": "fallback_policy", "value": "fallback_identity", "label": "人物长相匹配时兜底"}
                            ],
                        },
                    ],
                },
                "selection_eligibility": "fallback_identity_only",
            }
        )
        fields = {item.field: item.source for item in result.fields}
        self.assertIn("这张图最不能丢的特点是什么？：人物长相", fields["reference_roles"])
        self.assertEqual(fields["selection_eligibility"], "没有完全匹配时怎么办？：人物长相匹配时兜底")

    def test_review_prompt_contains_question_evidence_and_strict_schema(self) -> None:
        system_prompt, user_prompt = build_reference_metadata_review_prompt(
            {
                "version": 2,
                "answers": [
                    {
                        "id": "core_anchor",
                        "question": "这张图最不能丢的特点是什么？",
                        "selections": [{"field": "core_anchor", "value": "identity", "label": "人物长相"}],
                    }
                ],
            },
            {"preserve": ["identity"]},
            available_presets=["自拍"],
        )
        self.assertIn("交叉审批", system_prompt)
        self.assertIn("不要发明", system_prompt)
        self.assertIn("这张图最不能丢的特点是什么？", user_prompt)
        self.assertIn('"responsibility_decisions"', user_prompt)

    def test_expert_override_is_explicit_and_keeps_questionnaire(self) -> None:
        result = compile_reference_metadata(
            {
                "preserve": ["identity", "outfit"],
                "outfit_behavior": "preserve_unless_explicit_change",
                "outfit_category": "sleepwear",
                "manual_override": {
                    "reference_roles": ["identity"],
                    "scene_categories": ["home"],
                    "selection_eligibility": "fallback_allowed",
                },
            }
        )
        self.assertEqual(result.metadata["reference_roles"], ["identity"])
        self.assertEqual(result.metadata["scene_categories"], ["home"])
        self.assertEqual(result.metadata["metadata_source"], "manual_override")
        self.assertEqual(result.metadata["editor_intent"]["manual_override"]["reference_roles"], ["identity"])
        sources = {item.field: item.source for item in result.fields}
        self.assertEqual(sources["reference_roles"], "manual_override")


if __name__ == "__main__":
    unittest.main()
