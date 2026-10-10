# -*- coding: utf-8 -*-
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "pages" / "companion-panel" / "app.js").read_text(encoding="utf-8")
MIRROR_APP_JS = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "pages" / "companion-panel" / "index.html").read_text(encoding="utf-8")
MIRROR_INDEX_HTML = (ROOT / "pages" / "陪伴面板" / "index.html").read_text(encoding="utf-8")


class UserCreationUiTests(unittest.TestCase):
    def test_add_user_creates_a_profile_without_removed_private_permission_field(self) -> None:
        match = re.search(
            r'\$\("#addUserForm"\).*?postJson\("/user/update",\s*\{(?P<payload>.*?)\}\)',
            APP_JS,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        payload = match.group("payload")
        self.assertIn("user_id: userId", payload)
        self.assertIn('nickname: form.get("nickname")', payload)
        self.assertNotIn("enabled", payload)
        self.assertNotIn("private_companion_enabled", payload)
        self.assertIn("已添加用户档案", APP_JS)

    def test_panel_mirrors_remain_identical(self) -> None:
        self.assertEqual(APP_JS, MIRROR_APP_JS)
        self.assertEqual(INDEX_HTML, MIRROR_INDEX_HTML)
        self.assertIn("user-create=profile-v1", INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
