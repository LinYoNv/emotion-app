# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import configparser
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _tree(filename: str) -> ast.Module:
    return ast.parse((ROOT / filename).read_text(encoding="utf-8"), filename=filename)


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)


def _method(owner: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _span(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno")) - int(getattr(node, "lineno")) + 1


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_main_entrypoints_remain_thin_adapters(self) -> None:
        tree = _tree("main.py")
        plugin = _class(tree, "PrivateCompanionPlugin")

        initializer = _method(plugin, "__init__")
        state_injection = _method(plugin, "inject_humanized_state")
        private_handler = _method(plugin, "on_private_message")
        group_handler = _method(plugin, "on_group_message")

        self.assertLessEqual(_span(initializer), 20)
        self.assertLessEqual(_span(state_injection), 8)
        self.assertLessEqual(_span(private_handler), 8)
        self.assertLessEqual(_span(group_handler), 8)
        self.assertIn("initialize_plugin_config", ast.unparse(initializer))
        self.assertIn("run_humanized_state_injection", ast.unparse(state_injection))
        self.assertIn("handle_private_message", ast.unparse(private_handler))
        self.assertIn("handle_group_message", ast.unparse(group_handler))

    def test_page_setting_dispatcher_stays_domain_oriented(self) -> None:
        tree = _tree("page_api_settings.py")
        owner = _class(tree, "PageSettingNormalizerMixin")
        dispatcher = _method(owner, "_normalize_setting_value")
        handlers = [
            node
            for node in owner.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_normalize_page_")
        ]

        self.assertLessEqual(_span(dispatcher), 20)
        self.assertEqual(len(handlers), 6)
        self.assertTrue(all(_span(handler) <= 320 for handler in handlers))

    def test_daily_tick_keeps_per_user_execution_outside_state_module(self) -> None:
        state_tree = _tree("daily_state.py")
        state_owner = _class(state_tree, "DailyStateMixin")
        tick = _method(state_owner, "_tick")
        bases = {ast.unparse(base) for base in state_owner.bases}

        engine_tree = _tree("daily_state_tick.py")
        engine_owner = _class(engine_tree, "DailyStateTickMixin")
        user_tick = _method(engine_owner, "_tick_user")

        self.assertLessEqual(_span(tick), 80)
        self.assertIn("DailyStateTickMixin", bases)
        self.assertGreater(_span(user_tick), 0)
        self.assertIn("await self._tick_user", ast.unparse(tick))

    def test_proactive_rest_gate_has_one_implementation_owner(self) -> None:
        tree = _tree("proactive.py")
        proactive = _class(tree, "ProactiveMixin")
        bases = {ast.unparse(base) for base in proactive.bases}
        owned_methods = {
            node.name
            for node in proactive.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        rest_methods = {
            "_apply_user_rest_silence_from_message",
            "_detect_user_rest_silence_until",
            "_next_user_rest_morning_ts",
            "_user_rest_signal_should_block_current_reply",
            "_user_rest_silence_until",
            "_user_rest_text_is_meta_discussion",
            "_user_rest_text_is_quoted_or_report",
        }

        self.assertIn("UserRestGateMixin", bases)
        self.assertFalse(rest_methods & owned_methods)

    def test_pytest_collection_ignores_verification_snapshots(self) -> None:
        config = configparser.ConfigParser()
        config.read(ROOT / "pytest.ini", encoding="utf-8")

        self.assertEqual(config["pytest"]["testpaths"].strip(), "tests")
        ignored = set(config["pytest"]["norecursedirs"].split())
        self.assertIn("verification", ignored)


if __name__ == "__main__":
    unittest.main()
