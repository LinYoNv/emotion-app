from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = ROOT / "metadata.yaml"


class MetadataEncodingTests(unittest.TestCase):
    def test_metadata_is_utf8_without_bom(self) -> None:
        raw = METADATA_PATH.read_bytes()

        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "metadata.yaml must not contain a UTF-8 BOM")
        text = raw.decode("utf-8")
        self.assertEqual(raw, text.encode("utf-8"))

    def test_metadata_keeps_expected_chinese_text(self) -> None:
        text = METADATA_PATH.read_text(encoding="utf-8")

        self.assertIn("display_name: 我会牢牢记住你", text)
        self.assertNotIn("�", text)


if __name__ == "__main__":
    unittest.main()
