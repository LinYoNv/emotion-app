"""旧版「世界知识」表单里的世界蓝图必须迁进角色档案。

这几段（所处世界/时代背景/基本法则/特殊规则/主要活动场景/世界观关系网）原先被当成
关系内容排除，迁移后只剩角色本体，世界设定整段丢失。
"""

from __future__ import annotations

import unittest

from companion_v2.role_profile.configuration import FIELD_LIMITS
from companion_v2.role_profile.migration import (
    RoleProfileMigrationError,
    map_v1_world_knowledge_role_profile,
)

WORLD_PERSONA = """**姓名**：澄溪
**种族**：人类
**所处世界**：星海列岛
**时代背景**：蒸汽与潮汐魔法并存的航海时代
**基本法则/基调**：潮汐驱动一切机械，夜晚海水会发光
**特殊规则**：夜间禁止出海
**主要活动场景**：灯塔、港口、旧书店
**世界观关系网**：潮汐议会掌权，灯塔守望者保持中立
"""


class WorldBlueprintMigrationTests(unittest.TestCase):
    def test_worldview_text_is_read_separately_from_the_role_form(self):
        # 旧版把「世界蓝图」单独存一份：只传角色正文时世界字段是空的。
        role_only = "**姓名**：澄溪\n**性格描述**：慢热。\n"
        without_world = map_v1_world_knowledge_role_profile(role_only)
        self.assertNotIn("world_name", without_world["fields"])
        merged = map_v1_world_knowledge_role_profile(
            role_only, worldview_text="**所处世界**：星海列岛\n**特殊规则**：夜间禁止出海\n")
        self.assertEqual(merged["fields"]["world_name"], "星海列岛")
        self.assertEqual(merged["fields"]["rules"], "夜间禁止出海")
        self.assertEqual(merged["fields"]["name"], "澄溪")

    def test_world_sections_land_in_the_world_blueprint_fields(self):
        result = map_v1_world_knowledge_role_profile(WORLD_PERSONA)
        fields = result["fields"]
        self.assertEqual(fields["world_name"], "星海列岛")
        self.assertIn("潮汐魔法", fields["era"])
        self.assertIn("潮汐驱动", fields["tone"])
        self.assertIn("夜间禁止出海", fields["rules"])
        self.assertIn("灯塔", fields["scenes"])
        self.assertIn("潮汐议会", fields["network"])
        self.assertEqual(result["excluded_relationship_sections"], 0)

    def test_relationship_sections_are_still_excluded(self):
        persona = WORLD_PERSONA + "**用户**：小满\n**对用户的称呼**：阿满\n**与用户关系**：恋人\n"
        result = map_v1_world_knowledge_role_profile(persona)
        self.assertNotIn("小满", "".join(result["fields"].values()))
        self.assertGreaterEqual(result["excluded_relationship_sections"], 1)

    def test_overlong_world_section_is_rejected_not_truncated(self):
        persona = "**所处世界**：" + "星" * (FIELD_LIMITS["world_name"] + 1) + "\n"
        result = map_v1_world_knowledge_role_profile(persona)
        self.assertIn("world_name", result["rejected_fields"])
        self.assertNotIn("world_name", result["fields"])

    def test_world_only_persona_is_not_an_error(self):
        # 只有世界设定也算有效档案，不能报「没有可迁移字段」。
        result = map_v1_world_knowledge_role_profile("**所处世界**：星海列岛\n")
        self.assertEqual(result["fields"]["world_name"], "星海列岛")

    def test_persona_without_supported_sections_still_raises(self):
        with self.assertRaises(RoleProfileMigrationError):
            map_v1_world_knowledge_role_profile("**用户**：小满\n")


if __name__ == "__main__":
    unittest.main()
