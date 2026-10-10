import unittest
from pathlib import Path


class BookshelfDiaryReaderUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.script = (root / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        cls.css = (root / "pages" / "陪伴面板" / "app.css").read_text(encoding="utf-8")

    def test_diary_entries_use_stable_newest_first_sorting(self) -> None:
        self.assertIn("function sortedDiaryEntries(entries)", self.script)
        self.assertIn("const entries = sortedDiaryEntries(book.entries);", self.script)
        self.assertIn("const latest = entries[0] || {};", self.script)
        self.assertIn("const rows = sortedDiaryEntries(entries);", self.script)

    def test_diary_reader_distinguishes_entries_from_days(self) -> None:
        self.assertIn("`${rows.length} 篇 · ${uniqueDayCount} 天`", self.script)
        self.assertIn("`${rows.length} 篇`", self.script)
        self.assertIn("function diaryEntryTimeLabel(entry)", self.script)
        self.assertIn('class="diary-date-list-head"', self.script)
        self.assertIn('aria-current="${String(entry.entry_key', self.script)

    def test_diary_reader_keeps_summaries_and_actions_readable(self) -> None:
        self.assertIn("-webkit-line-clamp: 2;", self.css)
        self.assertIn(".diary-date-list button:focus-visible", self.css)
        self.assertIn(".diary-paper .reader-page-foot .danger-outline", self.css)
        self.assertIn("min-height: clamp(460px, 62vh, 680px);", self.css)


if __name__ == "__main__":
    unittest.main()
