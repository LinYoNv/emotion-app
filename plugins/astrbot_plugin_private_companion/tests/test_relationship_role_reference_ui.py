# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RelationshipRoleReferenceUiTests(unittest.TestCase):
    def test_both_panel_scripts_expose_role_reference_asset_workflow(self) -> None:
        scripts = [
            (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8"),
            (ROOT / "pages" / "companion-panel" / "app.js").read_text(encoding="utf-8"),
        ]
        for script in scripts:
            self.assertIn('const RELATIONSHIP_ROLE_REFERENCE_SCOPE = "relation_role";', script)
            self.assertIn('function relationshipRoleOwnerKey(name)', script)
            self.assertIn('data-relationship-role-reference-upload', script)
            self.assertIn('data-relationship-role-reference-delete', script)
            self.assertIn('/reference_asset/list?', script)
            self.assertIn('/reference_asset/upload', script)
            self.assertIn('/reference_asset/image_data', script)
            self.assertIn('/reference_asset/delete', script)
            self.assertIn('owner_id: next', script)
            self.assertIn('{ reload: false }', script)
            self.assertIn("仅在明确提到该角色或要求合影时使用", script)

    def test_panel_scripts_stay_in_sync(self) -> None:
        utf8_script = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        ascii_script = (ROOT / "pages" / "companion-panel" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(utf8_script, ascii_script)


if __name__ == "__main__":
    unittest.main()
