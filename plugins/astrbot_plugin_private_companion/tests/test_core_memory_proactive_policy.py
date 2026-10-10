# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from astrbot_plugin_private_companion.memory_context_policy import core_memory_usage_contract


CORE_CONTEXT = """<core_memory>
<memory label="health" kind="state" priority="80">用户需要每天提醒吃药</memory>
</core_memory>"""


class CoreMemoryProactivePolicyTests(unittest.TestCase):
    def test_non_core_memory_context_does_not_add_contract(self) -> None:
        self.assertEqual("", core_memory_usage_contract("【稳定记忆】喜欢红茶", stage="review"))

    def test_review_contract_prevents_core_memory_from_becoming_trigger(self) -> None:
        contract = core_memory_usage_contract(CORE_CONTEXT, stage="review")

        self.assertIn("不能单独充当现实触发证据", contract)
        self.assertIn("不得仅凭核心记忆新建主动候选", contract)
        self.assertIn("不得当作当前状态", contract)
        self.assertIn("无法在原路线内消除冲突时再 defer/drop", contract)

    def test_generation_contract_preserves_existing_plan(self) -> None:
        contract = core_memory_usage_contract(CORE_CONTEXT, stage="generation")

        self.assertIn("不得改变既定 reason/action/topic/motive", contract)
        self.assertIn("不得把无关计划转成提醒", contract)
        self.assertIn("当前用户原文、可靠实时信息冲突时，以当前证据为准", contract)


if __name__ == "__main__":
    unittest.main()
