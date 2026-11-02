"""旧版配置里的「用户档案」提示词要能迁进 V2 关系档案。

用户库只有昵称/关系等级/备注；性别、生日、职业、相处方式只存在这份提示词里，
不读它就永远是空的。
"""

from __future__ import annotations

import unittest

from companion_v2.legacy.relationship_profiles import config_relationship_profile
from companion_v2.relationships.profile import FIELD_LIMITS

PROMPT = """对用户的称呼：比折
用户性别：男
用户生日：2002-10-24
用户职业：开发者
是角色的XX：非常特殊、非常亲近，被诺星缘信任和依赖的人。
与角色的相处方式：作者兼监护人，温柔听话但偶尔会小声反驳。
其他补充信息：可以通过投影把现实世界的东西投递过来。
"""


class ConfigRelationshipProfileTests(unittest.TestCase):
    def test_prompt_fields_map_to_the_v2_profile(self):
        seed = config_relationship_profile(PROMPT, primary_user_id="995051631")
        self.assertEqual(seed["user_id"], "995051631")
        self.assertEqual(seed["fields"], {
            "default_nickname": "比折",
            "user_gender": "男",
            "user_birthday": "2002-10-24",
            "user_occupation": "开发者",
            "role_relation": "非常特殊、非常亲近，被诺星缘信任和依赖的人。",
            "interaction": "作者兼监护人，温柔听话但偶尔会小声反驳。",
            "extra": "可以通过投影把现实世界的东西投递过来。",
        })
        self.assertTrue(any("roleplay_user_profile_prompt" in item for item in seed["source_fields"]))

    def test_continuation_lines_belong_to_the_previous_field(self):
        seed = config_relationship_profile("与角色的相处方式：第一行\n第二行\n用户职业：开发者\n")
        self.assertEqual(seed["fields"]["interaction"], "第一行\n第二行")
        self.assertEqual(seed["fields"]["user_occupation"], "开发者")

    def test_overlong_field_is_dropped_not_truncated(self):
        long_gender = "男" * (FIELD_LIMITS["user_gender"] + 1)
        seed = config_relationship_profile(f"用户性别：{long_gender}\n用户职业：开发者\n")
        self.assertNotIn("user_gender", seed["fields"])
        self.assertIn("user_gender", seed["dropped"])
        self.assertEqual(seed["fields"]["user_occupation"], "开发者")

    def test_empty_or_unknown_text_yields_nothing(self):
        self.assertIsNone(config_relationship_profile(""))
        self.assertIsNone(config_relationship_profile("这里没有任何已知标签"))
        self.assertIsNone(config_relationship_profile(None))

    def test_fullwidth_and_halfwidth_colons_both_work(self):
        seed = config_relationship_profile("用户性别: 女\n用户职业：学生\n")
        self.assertEqual(seed["fields"]["user_gender"], "女")
        self.assertEqual(seed["fields"]["user_occupation"], "学生")


if __name__ == "__main__":
    unittest.main()
