# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeleteButtonUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scripts = [
            (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8"),
            (ROOT / "pages" / "companion-panel" / "app.js").read_text(encoding="utf-8"),
        ]

    def test_panel_scripts_stay_in_sync(self) -> None:
        self.assertEqual(self.scripts[0], self.scripts[1])

    def test_confirmation_has_visible_and_accessible_state(self) -> None:
        script = self.scripts[0]
        self.assertIn('control.classList.add("is-confirming")', script)
        self.assertIn('control.removeAttribute("aria-pressed")', script)
        self.assertIn('control.setAttribute("aria-busy", "true")', script)

    def test_image_cache_delete_is_resolved_before_row_click(self) -> None:
        script = self.scripts[0]
        delete_index = script.index('const deleteButton = element?.closest("[data-image-cache-delete]")')
        row_index = script.index('const row = element?.closest("[data-image-cache-key]")')
        self.assertLess(delete_index, row_index)
        self.assertIn("event.stopPropagation();", script[delete_index:delete_index + 500])

    def test_destructive_detail_actions_are_explicit_buttons(self) -> None:
        script = self.scripts[0]
        self.assertIn('<button type="button" data-user-action="delete"', script)
        self.assertIn('<button type="button" data-group-action="delete"', script)

    def test_worldbook_delete_uses_shared_confirmation(self) -> None:
        script = self.scripts[0]
        self.assertIn('requireSecondClick(button, `worldbook-delete:${userId}`', script)
        self.assertIn('requireSecondClick(button, `worldbook-group-delete:${groupId}`', script)
        self.assertNotIn('data.deleteArmed', script)


if __name__ == "__main__":
    unittest.main()
