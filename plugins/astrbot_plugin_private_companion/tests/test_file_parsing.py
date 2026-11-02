"""附件解析的真实格式样本：每一种支持的后缀都过一遍提取，不只验后缀选择。"""

from __future__ import annotations

import importlib.util
import io
import unittest
import zipfile

from astrbot_host.files.parsing import extract_document, parser_availability
from companion_v2.execution.models import TaskError


def has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def docx_bytes() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("第一段正文")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "姓名"
    table.cell(0, 1).text = "小凌"
    table.cell(1, 0).text = "年龄"
    table.cell(1, 1).text = "20"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "账单"
    sheet.append(["项目", "金额"])
    sheet.append(["咖啡", 18])
    other = workbook.create_sheet("备注")
    other["A1"] = "第二张表"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pptx_bytes() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "周会要点"
    box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    box.text_frame.text = "下周继续"
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


class TextExtractionTests(unittest.TestCase):
    def test_utf8_and_gb18030_text_are_read(self):
        self.assertIn("你好", extract_document("你好，世界".encode("utf-8"), 0, "a.txt").text)
        self.assertIn("你好", extract_document("你好，世界".encode("gb18030"), 0, "b.txt").text)

    def test_binary_masquerading_as_text_is_rejected(self):
        with self.assertRaises(TaskError) as caught:
            extract_document("abc\x00def".encode("utf-8"), 0, "c.txt")
        self.assertEqual(caught.exception.code, "host_file_invalid")

    def test_unsupported_or_unselected_format_is_refused(self):
        with self.assertRaises(TaskError) as caught:
            extract_document(b"PK\x03\x04", 0, "d.exe")
        self.assertEqual(caught.exception.code, "host_file_format_unsupported")
        with self.assertRaises(TaskError) as caught:
            extract_document(b"plain", 0, "e.txt", formats=frozenset({".pdf"}))
        self.assertEqual(caught.exception.code, "host_file_format_unsupported")


class DocumentExtractionTests(unittest.TestCase):
    @unittest.skipUnless(has("docx"), "python-docx 未安装")
    def test_docx_keeps_paragraphs_and_table_cells(self):
        text = extract_document(docx_bytes(), 0, "note.docx").text
        self.assertIn("第一段正文", text)
        self.assertIn("姓名 | 小凌", text)
        self.assertIn("年龄 | 20", text)

    @unittest.skipUnless(has("openpyxl"), "openpyxl 未安装")
    def test_xlsx_marks_each_sheet_and_keeps_cells(self):
        text = extract_document(xlsx_bytes(), 0, "book.xlsx").text
        self.assertIn("[工作表 账单]", text)
        self.assertIn("咖啡 | 18", text)
        self.assertIn("[工作表 备注]", text)
        self.assertIn("第二张表", text)

    @unittest.skipUnless(has("pptx"), "python-pptx 未安装")
    def test_pptx_marks_slides_and_reads_text_frames(self):
        text = extract_document(pptx_bytes(), 0, "deck.pptx").text
        self.assertIn("[幻灯片 1]", text)
        self.assertIn("周会要点", text)
        self.assertIn("下周继续", text)

    @unittest.skipUnless(has("docx"), "python-docx 未安装")
    def test_truncation_is_recorded_instead_of_silently_cut(self):
        result = extract_document(docx_bytes(), 0, "note.docx", maximum_chars=6)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.text), 6)
        self.assertEqual(len(result.content_hash), 64)

    @unittest.skipUnless(has("docx"), "python-docx 未安装")
    def test_broken_document_is_reported_as_invalid(self):
        with self.assertRaises(TaskError) as caught:
            extract_document(b"not a zip", 0, "broken.docx")
        self.assertEqual(caught.exception.code, "host_file_invalid")

    def test_oversized_original_is_refused_before_parsing(self):
        with self.assertRaises(TaskError) as caught:
            extract_document(b"x" * (16 * 1024 * 1024 + 1), 0, "big.txt")
        self.assertEqual(caught.exception.code, "host_file_limit")


class ParserAvailabilityTests(unittest.TestCase):
    def test_every_document_suffix_reports_a_state(self):
        available = parser_availability()
        self.assertEqual(set(available), {".pdf", ".docx", ".xlsx", ".pptx"})
        self.assertTrue(all(type(value) is bool for value in available.values()))

    def test_missing_parser_surfaces_as_decoder_unavailable(self):
        # 缺组件时必须给出 host_file_decoder_unavailable，而不是把它当成文件损坏。
        missing = [suffix for suffix, ok in parser_availability().items() if not ok]
        if not missing:
            self.skipTest("本机解析器齐全")
        suffix = missing[0]
        with self.assertRaises(TaskError) as caught:
            extract_document(b"PK\x03\x04", 0, "file" + suffix)
        self.assertIn(caught.exception.code, ("host_file_decoder_unavailable", "host_file_invalid"))


if __name__ == "__main__":
    unittest.main()
