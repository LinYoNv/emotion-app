# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BookshelfAccessUiTests(unittest.TestCase):
    def test_both_panel_scripts_persist_and_restore_daily_access(self) -> None:
        scripts = [
            ROOT / "pages" / "companion-panel" / "app.js",
            ROOT / "pages" / "陪伴面板" / "app.js",
        ]
        for path in scripts:
            script = path.read_text(encoding="utf-8")
            self.assertIn('const BOOKSHELF_ACCESS_STORAGE_KEY = "pc_bookshelf_access_v1";', script)
            self.assertIn("function persistBookshelfAccess(bookshelf = {})", script)
            self.assertIn("async function restoreBookshelfAccess()", script)
            self.assertIn('postJson("/bookshelf/session", {', script)
            self.assertIn("_persona_id: requestPersonaId", script)
            self.assertIn("localStorage.setItem(BOOKSHELF_ACCESS_STORAGE_KEY", script)
            self.assertIn("void restoreBookshelfAccess();", script)

    def test_diary_delete_and_unlocked_snapshot_are_persona_scoped(self) -> None:
        scripts = [
            ROOT / "pages" / "companion-panel" / "app.js",
            ROOT / "pages" / "陪伴面板" / "app.js",
        ]
        contents = [path.read_text(encoding="utf-8") for path in scripts]
        self.assertEqual(contents[0], contents[1])
        for script in contents:
            self.assertIn("function bookshelfUnlockedForCurrentPersona()", script)
            self.assertIn("state.bookshelfPersonaId !== personaId", script)
            self.assertIn("personaId: activeBookshelfPersonaId()", script)
            self.assertIn("resetBookshelfSelection();", script)
            self.assertIn("data-book-entry-key=", script)
            self.assertIn("entry_key: diaryEntryKey", script)
            self.assertIn("function selectDiaryEntry(value)", script)

    def test_session_restore_discards_response_after_persona_switch(self) -> None:
        scripts = [
            ROOT / "pages" / "companion-panel" / "app.js",
            ROOT / "pages" / "陪伴面板" / "app.js",
        ]
        for path in scripts:
            script = path.read_text(encoding="utf-8")
            start = script.index("async function restoreBookshelfAccess()")
            end = script.index("\nfunction formatBookContent", start)
            restore = script[start:end]
            response_guard = "if (activeBookshelfPersonaId() !== requestPersonaId) return false;"
            self.assertIn("const requestPersonaId = activeBookshelfPersonaId();", restore)
            self.assertEqual(2, restore.count(response_guard))
            self.assertLess(restore.index(response_guard), restore.index("setBookshelfUnlocked(bookshelf);"))
            self.assertNotIn("state.bookshelfAccessToken = stored.token;\n  try", restore)
            self.assertIn("_persona_id: requestPersonaId", restore)


if __name__ == "__main__":
    unittest.main()
