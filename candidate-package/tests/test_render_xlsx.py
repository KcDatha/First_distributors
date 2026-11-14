"""Tests for rfqparse.render.xlsx using workbooks built here (none are copies of dev data).

    python tests/test_render_xlsx.py -v
"""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.comments import Comment
from openpyxl.styles import Font

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rfqparse.render import xlsx as X  # noqa: E402


def to_bytes(wb: Workbook) -> bytes:
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def render(wb: Workbook):
    return X.render_xlsx(to_bytes(wb))


def _wb_with(v):
    wb = Workbook()
    wb.active["A1"] = v
    return wb


def _plain_zip() -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("hello.txt", "hi")
    return b.getvalue()


class StructureTests(unittest.TestCase):
    def test_sheet_order_names_headings_notes_and_row_numbers(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "First"
        ws["A1"] = "Project Heading"
        ws["A2"] = "NOTE: all items zinc coated unless noted"
        ws["A4"] = "item"
        ws["B4"] = "qty"
        ws["A5"] = "widget"
        ws["B5"] = 7.0
        wb.create_sheet("Second")["A1"] = "second sheet text"
        r = render(wb)
        t = r.text
        self.assertTrue(r.ok)
        self.assertLess(t.index("SHEET: First"), t.index("SHEET: Second"))
        self.assertIn("R1: Project Heading", t)
        self.assertIn("R2: NOTE: all items zinc coated unless noted", t)
        self.assertIn("R5: widget | 7", t)          # 7.0 shown as 7
        self.assertNotIn("R3:", t)                  # blank rows skipped, row numbers still true
        self.assertLess(t.index("R2:"), t.index("R4:"))

    def test_hidden_sheet_labelled_not_dropped(self):
        wb = Workbook()
        wb.active.title = "Current"
        wb.active["A1"] = "visible stuff"
        old = wb.create_sheet("Old")
        old["A1"] = "superseded stuff"
        old.sheet_state = "hidden"
        t = render(wb).text
        self.assertIn("SHEET: Old  [HIDDEN SHEET]", t)
        self.assertIn("superseded stuff", t)
        self.assertIn("=== SHEET: Current ===", t)    # visible sheet carries no label

    def test_very_hidden_sheet(self):
        wb = Workbook()
        wb.active["A1"] = "x"
        s = wb.create_sheet("Secret")
        s["A1"] = "y"
        s.sheet_state = "veryHidden"
        self.assertIn("[HIDDEN SHEET (very hidden)]", render(wb).text)

    def test_hidden_row_and_column_labelled(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["a", "b", "c"])
        ws.append(["d", "e", "f"])
        ws.row_dimensions[2].hidden = True
        ws.column_dimensions["B"].hidden = True
        t = render(wb).text
        self.assertIn("R2 [HIDDEN ROW]: d | [HIDDEN COL] e | f", t)
        self.assertIn("B [HIDDEN COL]", t)
        self.assertIn("R1: a | [HIDDEN COL] b | c", t)


class MergeTests(unittest.TestCase):
    def test_vertical_merge_pushes_value_into_covered_rows(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "ALL PIPE SCH 40"
        ws.merge_cells("A1:A3")
        ws["B1"] = "2 in"
        ws["B2"] = "3 in"
        ws["B3"] = "4 in"
        ws["A4"] = "unrelated"
        ws["B4"] = "x"
        t = render(wb).text
        self.assertIn("R1: ALL PIPE SCH 40 | 2 in", t)
        self.assertIn("R2: [merged] ALL PIPE SCH 40 | 3 in", t)
        self.assertIn("R3: [merged] ALL PIPE SCH 40 | 4 in", t)
        self.assertIn("R4: unrelated | x", t)       # context does not leak past the merge

    def test_horizontal_merge_shown_once(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "SECTION HEADING"
        ws.merge_cells("A1:D1")
        ws["A2"] = "x"
        t = render(wb).text
        self.assertEqual(t.count("SECTION HEADING"), 1)
        self.assertIn("R1: SECTION HEADING", t)

    def test_block_merge_value_once_per_covered_row(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "BLOCK"
        ws.merge_cells("A1:B2")
        t = render(wb).text
        self.assertEqual(t.count("BLOCK"), 2)       # start row + one pushed row, not four copies
        self.assertIn("R2: [merged] BLOCK", t)


class StrikeFormulaTests(unittest.TestCase):
    def test_struck_cell_wrapped(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "keep me"
        ws["A2"] = "dropped item"
        ws["A2"].font = Font(strike=True)
        t = render(wb).text
        self.assertIn("R1: keep me", t)
        self.assertIn("R2: [STRUCK: dropped item]", t)

    def test_partial_strike_in_rich_text(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = CellRichText("keep ", TextBlock(InlineFont(strike=True), "old part"), " end")
        self.assertIn("keep [STRUCK: old part] end", render(wb).text)

    def test_formula_without_cached_value_shows_formula_not_none(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = 5
        ws["A2"] = 6
        ws["A3"] = "=SUM(A1:A2)"      # openpyxl writes no cached value
        t = render(wb).text
        self.assertIn("[formula, no cached value: =SUM(A1:A2)]", t)
        self.assertNotIn("None", t)

    def test_comment_included(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "pipe"
        ws["A1"].comment = Comment("customer wants galvanized", "x")
        self.assertIn("[COMMENT: customer wants galvanized]", render(wb).text)


class CapTests(unittest.TestCase):
    def test_row_cap_says_so(self):
        wb = Workbook()
        ws = wb.active
        for i in range(1, 31):
            ws.append([f"row{i}"])
        with mock.patch.object(X, "MAX_ROWS_PER_SHEET", 10):
            r = render(wb)
        self.assertTrue(r.truncated)
        self.assertIn("row10", r.text)
        self.assertNotIn("row11", r.text)
        self.assertIn("[TRUNCATED:", r.text)

    def test_char_cap_lists_sheets_not_shown(self):
        wb = Workbook()
        wb.active.title = "One"
        wb.active.append(["x" * 200])
        wb.create_sheet("Two").append(["y"])
        with mock.patch.object(X, "MAX_TOTAL_CHARS", 500):
            r = render(wb)
        self.assertTrue(r.truncated)
        self.assertIn("sheets not shown: Two", r.text)

    def test_long_cell_cut_and_marked(self):
        wb = Workbook()
        wb.active["A1"] = "z" * 2000
        self.assertIn("[cell cut]", render(wb).text)

    def test_untruncated_is_not_flagged(self):
        wb = Workbook()
        wb.active["A1"] = "small"
        self.assertFalse(render(wb).truncated)

    def test_oversized_file_refused_with_reason(self):
        good = to_bytes(_wb_with("hello"))
        with mock.patch.object(X, "MAX_BYTES", 10):
            r = X.render_xlsx(good)
        self.assertEqual(r.status, "unreadable")
        self.assertIn("too large", r.reason)


class UnreadableTests(unittest.TestCase):
    def test_never_raises_and_reports_reason(self):
        good = to_bytes(_wb_with("hello"))
        cases = {
            "empty": b"",
            "garbage": b"this is not a spreadsheet at all",
            "truncated zip": good[: len(good) // 2],
            "zip without workbook": _plain_zip(),
            "legacy/encrypted OLE": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
        }
        for name, blob in cases.items():
            with self.subTest(name):
                r = X.render_xlsx(blob)
                self.assertEqual(r.status, "unreadable")
                self.assertFalse(r.ok)
                self.assertTrue(r.reason)
                self.assertEqual(r.text, "")

    def test_non_bytes_input_does_not_raise(self):
        self.assertEqual(X.render_xlsx(None).status, "unreadable")


if __name__ == "__main__":
    unittest.main()
