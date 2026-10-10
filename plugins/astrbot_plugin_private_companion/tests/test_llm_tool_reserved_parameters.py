# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _is_llm_tool(function: ast.AsyncFunctionDef) -> bool:
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        if isinstance(target, ast.Attribute) and target.attr == "llm_tool":
            return True
    return False


class LlmToolReservedParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.tools = {
            node.name: node
            for node in ast.walk(cls.tree)
            if isinstance(node, ast.AsyncFunctionDef) and _is_llm_tool(node)
        }

    def test_llm_tools_do_not_expose_framework_context_parameter(self) -> None:
        conflicts = []
        for name, function in self.tools.items():
            argument_names = {
                argument.arg
                for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
            }
            if "context" in argument_names:
                conflicts.append(name)

        self.assertEqual([], conflicts)

    def test_reaction_lookup_uses_search_context_and_maps_it_internally(self) -> None:
        function = self.tools["pc_find_reaction_image"]
        argument_names = {argument.arg for argument in function.args.args}
        docstring = ast.get_docstring(function) or ""
        function_source = ast.get_source_segment(self.source, function) or ""

        self.assertIn("search_context", argument_names)
        self.assertIn("search_context(string)", docstring)
        self.assertNotRegex(docstring, r"(?m)^\s*context\(string\):")
        self.assertIn("context=search_context", function_source)
        self.assertIn("search_context=search_context", function_source)


if __name__ == "__main__":
    unittest.main()
