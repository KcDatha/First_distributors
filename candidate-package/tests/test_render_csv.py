"""Tests for rfqparse.render.csv_ with CSV bytes built here.

    python tests/test_render_csv.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rfqparse.render import csv_ as C  # noqa: E402


class CsvTests(unittest.TestCase):
    def test_header_kept_and_rows_numbered(self):
        r = C.render_csv(b"qty,size,item\n4,2in,elbow\n6,3in,tee\n")
        self.assertTrue(r.ok)
        self.assertIn("R1: qty | size | item", r.text)
        self.assertIn("R2: 4 | 2in | elbow", r.text)
        self.assertIn("R3: 6 | 3in | tee", r.text)
        self.assertFalse(r.truncated)

    def test_utf8_bom_stripped(self):
        r = C.render_csv("﻿qty,item\n1,café fitting\n".encode("utf-8"))
        self.assertIn("R1: qty | item", r.text)          # no BOM glued onto the first header
        self.assertIn("café fitting", r.text)
        self.assertEqual(r.warnings, [])

    def test_cp1252_fallback(self):
        r = C.render_csv(b"qty,item\n2,\x93quoted\x94 fitting\n")
        self.assertIn("“quoted” fitting", r.text)
        self.assertTrue(any("cp1252" in w for w in r.warnings))

    def test_utf16_with_bom(self):
        r = C.render_csv("qty,item\n1,tee\n".encode("utf-16"))
        self.assertIn("R2: 1 | tee", r.text)

    def test_delimiters_sniffed(self):
        for d, label in ((";", "';'"), ("\t", "TAB"), ("|", "'|'"), (",", "','")):
            with self.subTest(label):
                body = f"qty{d}size{d}item\n4{d}2in{d}elbow\n6{d}3in{d}tee\n".encode()
                r = C.render_csv(body)
                self.assertIn("R2: 4 | 2in | elbow", r.text)
                self.assertIn(label, r.text.splitlines()[0])

    def test_comma_inside_quotes_stays_one_cell(self):
        r = C.render_csv(b'qty,item\n4,"elbow, 90 deg, SW"\n3,tee\n')
        self.assertIn("R2: 4 | elbow, 90 deg, SW", r.text)

    def test_semicolon_file_with_commas_in_text(self):
        r = C.render_csv(b"qty;item\n4;elbow, 90 deg\n2;tee, straight\n")
        self.assertIn("R2: 4 | elbow, 90 deg", r.text)

    def test_multiline_quoted_cell_is_one_record(self):
        r = C.render_csv(b'qty,item\n4,"line one\nline two"\n5,tee\n')
        self.assertIn("R2: 4 | line one / line two", r.text)
        self.assertIn("R3: 5 | tee", r.text)         # numbering counts records, not physical lines

    def test_blank_rows_skipped_numbers_kept(self):
        r = C.render_csv(b"a,b\n\n,\nx,y\n")
        self.assertIn("R1: a | b", r.text)
        self.assertIn("R4: x | y", r.text)
        self.assertNotIn("R2:", r.text)

    def test_row_cap_says_so(self):
        body = ("n,v\n" + "\n".join(f"{i},row{i}" for i in range(1, 40))).encode()
        with mock.patch.object(C, "MAX_ROWS", 10):
            r = C.render_csv(body)
        self.assertTrue(r.truncated)
        self.assertIn("[TRUNCATED:", r.text)
        self.assertNotIn("row30", r.text)

    def test_char_cap_says_so(self):
        body = ("n,v\n" + "\n".join(f"{i},{'x' * 50}" for i in range(200))).encode()
        with mock.patch.object(C, "MAX_TOTAL_CHARS", 600):
            r = C.render_csv(body)
        self.assertTrue(r.truncated)
        self.assertLessEqual(len(r.text), 700)
        self.assertIn("[TRUNCATED:", r.text)

    def test_unreadable_cases_never_raise(self):
        cases = {
            "empty": b"",
            "whitespace": b"  \n\n \t\n",
            "binary": b"\x00\x01\x02\xff\xfe binary \x00",
            "only blank cells": b",,\n,,\n",
            "oversized": b"a,b\n1,2\n",
        }
        for name, blob in cases.items():
            with self.subTest(name):
                if name == "oversized":
                    with mock.patch.object(C, "MAX_BYTES", 3):
                        r = C.render_csv(blob)
                else:
                    r = C.render_csv(blob)
                self.assertEqual(r.status, "unreadable")
                self.assertTrue(r.reason)
                self.assertEqual(r.text, "")

    def test_non_bytes_does_not_raise(self):
        self.assertEqual(C.render_csv(None).status, "unreadable")

    def test_oversized_field_returns_partial_not_crash(self):
        import csv
        old = csv.field_size_limit()
        csv.field_size_limit(50)
        try:
            r = C.render_csv(b'a,b\n1,2\n3,"' + b"x" * 500 + b'"\n')
        finally:
            csv.field_size_limit(old)
        self.assertTrue(r.ok)
        self.assertIn("R2: 1 | 2", r.text)
        self.assertIn("CSV PARSE ERROR", r.text)
        self.assertTrue(r.truncated)


if __name__ == "__main__":
    unittest.main()
