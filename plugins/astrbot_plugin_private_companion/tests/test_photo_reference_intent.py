from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_plugin_private_companion"
package = sys.modules.setdefault(PACKAGE_NAME, types.ModuleType(PACKAGE_NAME))
package.__path__ = [str(PLUGIN_ROOT)]
package.__package__ = PACKAGE_NAME

from astrbot_plugin_private_companion.photo_reference_intent import (
    REFERENCE_ROLES,
    analyze_reference_intent,
    analyze_indexed_reference_roles,
)


class PhotoReferenceIntentTests(unittest.TestCase):
    def test_role_specific_reference_exclusion_does_not_disable_all_references(self) -> None:
        outfit = analyze_reference_intent(
            "不用参考图的衣服，只参考脸",
            has_explicit_reference=True,
        )
        scene = analyze_reference_intent(
            "不要参考图片的背景，只参考人物",
            has_explicit_reference=True,
        )

        self.assertNotEqual(outfit.continuity_mode, "new_topic")
        self.assertEqual(outfit.requested_roles, ("identity",))
        self.assertIn("outfit", outfit.excluded_roles)
        self.assertNotEqual(scene.continuity_mode, "new_topic")
        self.assertEqual(scene.requested_roles, ("identity",))
        self.assertIn("scene", scene.excluded_roles)

    def test_role_exclusion_accepts_demonstrative_image_phrases(self) -> None:
        cases = (
            ("不要参考这张图的人物，只参考背景", "identity", ("scene",)),
            ("别参考这个图的衣服，只参考脸", "outfit", ("identity",)),
            ("无需参考那张图片里的动作，只参考画风", "pose", ("style",)),
        )

        for request, excluded_role, requested_roles in cases:
            with self.subTest(request=request):
                intent = analyze_reference_intent(
                    request,
                    has_explicit_reference=True,
                )
                self.assertEqual(intent.requested_roles, requested_roles)
                self.assertEqual(intent.excluded_roles, (excluded_role,))

    def test_character_reference_with_outfit_change_keeps_only_identity(self) -> None:
        intent = analyze_reference_intent(
            "按这个人物但换成冬装",
            has_explicit_reference=True,
        )

        self.assertEqual(intent.requested_roles, ("identity",))
        self.assertIn("outfit", intent.excluded_roles)
        self.assertEqual(intent.continuity_mode, "ambiguous")
        self.assertGreaterEqual(intent.confidence, 0.9)
        self.assertEqual(intent.source, "rule")

    def test_explicit_only_phrases_map_to_one_reference_role(self) -> None:
        cases = (
            ("只参考脸", "identity"),
            ("只参考这套衣服", "outfit"),
            ("只参考这个姿势", "pose"),
            ("只参考这个画风", "style"),
        )

        for request, role in cases:
            with self.subTest(request=request):
                intent = analyze_reference_intent(
                    request,
                    has_explicit_reference=True,
                )
                self.assertEqual(intent.requested_roles, (role,))
                self.assertEqual(intent.excluded_roles, ())
                self.assertGreaterEqual(intent.confidence, 0.9)

    def test_direct_outfit_and_pose_phrases_map_without_model_fallback(self) -> None:
        cases = (
            ("穿这套衣服", "outfit"),
            ("照这个动作", "pose"),
        )

        for request, role in cases:
            with self.subTest(request=request):
                intent = analyze_reference_intent(
                    request,
                    has_explicit_reference=True,
                )
                self.assertEqual(intent.requested_roles, (role,))
                self.assertEqual(intent.source, "rule")

    def test_selfie_identity_default_is_marked_as_optional_workflow_context(self) -> None:
        intent = analyze_reference_intent(
            "随手拍一张",
            workflow_kind="selfie",
        )

        self.assertEqual(intent.requested_roles, ("identity",))
        self.assertEqual(intent.source, "workflow_default")

    def test_negated_reference_role_is_excluded_not_requested(self) -> None:
        intent = analyze_reference_intent(
            "不要参考衣服，只参考脸",
            has_explicit_reference=True,
        )

        self.assertEqual(intent.requested_roles, ("identity",))
        self.assertEqual(intent.excluded_roles, ("outfit",))

    def test_explicitly_rejecting_character_reference_disables_selfie_identity_default(self) -> None:
        for request in ("不要使用人物参考", "不要人物参考"):
            with self.subTest(request=request):
                intent = analyze_reference_intent(
                    request,
                    workflow_kind="selfie",
                )

                self.assertEqual(intent.requested_roles, ())
                self.assertEqual(intent.excluded_roles, ("identity",))
                self.assertEqual(intent.continuity_mode, "ambiguous")
                self.assertEqual(intent.source, "rule")

    def test_fresh_image_phrases_exclude_every_reference_role(self) -> None:
        for request in ("不要参考图", "重新开始画", "生成全新画面"):
            with self.subTest(request=request):
                intent = analyze_reference_intent(
                    request,
                    has_explicit_reference=True,
                )
                self.assertEqual(intent.requested_roles, ())
                self.assertEqual(intent.excluded_roles, REFERENCE_ROLES)
                self.assertEqual(intent.continuity_mode, "new_topic")
                self.assertEqual(intent.confidence, 1.0)

    def test_continuation_with_pose_change_preserves_identity_outfit_and_scene(self) -> None:
        intent = analyze_reference_intent("接着上一张换个姿势")

        self.assertEqual(
            intent.requested_roles,
            ("identity", "outfit", "scene", "continuity"),
        )
        self.assertEqual(intent.excluded_roles, ("pose",))
        self.assertEqual(intent.continuity_mode, "continuation")
        self.assertGreaterEqual(intent.confidence, 0.9)

    def test_continuation_with_outfit_change_does_not_preserve_old_outfit(self) -> None:
        intent = analyze_reference_intent("接着上一张但换成睡衣")

        self.assertEqual(
            intent.requested_roles,
            ("identity", "scene", "continuity"),
        )
        self.assertEqual(intent.excluded_roles, ("outfit",))
        self.assertEqual(intent.continuity_mode, "continuation")

    def test_continuation_with_direct_new_scene_and_outfit_keeps_identity_only(self) -> None:
        intent = analyze_reference_intent("接着来一张海边泳装照")

        self.assertEqual(intent.requested_roles, ("identity", "continuity"))
        self.assertEqual(intent.excluded_roles, ("outfit", "scene"))
        self.assertEqual(intent.continuity_mode, "continuation")

    def test_new_image_can_still_request_only_the_reference_background(self) -> None:
        intent = analyze_reference_intent(
            "重新生成一张，只参考这张图的背景",
            has_explicit_reference=True,
        )

        self.assertEqual(intent.requested_roles, ("scene",))
        self.assertNotEqual(intent.continuity_mode, "new_topic")

    def test_edit_request_requires_source_reference(self) -> None:
        intent = analyze_reference_intent(
            "把这张改成动漫风",
            has_explicit_reference=True,
        )

        self.assertEqual(intent.requested_roles, ("source",))
        self.assertEqual(intent.excluded_roles, ("style",))
        self.assertEqual(intent.continuity_mode, "edit")
        self.assertGreaterEqual(intent.confidence, 0.9)

    def test_low_confidence_explicit_reference_keeps_identity_only(self) -> None:
        intent = analyze_reference_intent(
            "参考一下",
            has_explicit_reference=True,
        )

        self.assertEqual(intent.requested_roles, ("identity",))
        self.assertEqual(intent.excluded_roles, ())
        self.assertEqual(intent.continuity_mode, "ambiguous")
        self.assertLess(intent.confidence, 0.7)
        self.assertEqual(intent.source, "conservative")

    def test_ambiguous_scene_change_does_not_inherit_previous_scene(self) -> None:
        intent = analyze_reference_intent("换个地方")

        self.assertEqual(intent.requested_roles, ("identity",))
        self.assertEqual(
            intent.excluded_roles,
            ("outfit", "pose", "scene", "continuity"),
        )
        self.assertEqual(intent.continuity_mode, "ambiguous")

    def test_indexed_multi_image_roles_are_kept_separate(self) -> None:
        roles = analyze_indexed_reference_roles(
            "用第一张的脸，第二张的衣服，第三张的姿势",
            image_count=3,
        )

        self.assertEqual(
            roles,
            (("identity",), ("outfit",), ("pose",)),
        )
        intent = analyze_reference_intent(
            "用第一张的脸，第二张的衣服，第三张的姿势",
            has_explicit_reference=True,
        )
        self.assertEqual(intent.requested_roles, ("identity", "outfit", "pose"))


if __name__ == "__main__":
    unittest.main()
