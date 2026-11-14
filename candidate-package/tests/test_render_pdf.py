"""Tests for rfqparse.render.pdf with PDFs built here by hand (no PDF library involved),
plus a regression on three real dev attachments.

    python tests/test_render_pdf.py -v
"""

from __future__ import annotations

import hashlib
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rfqparse import config  # noqa: E402
from rfqparse.render import pdf as P  # noqa: E402

PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out, i, j = bytearray(), 0, 0
    for b in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_content(lines: list[str], top: int = 740) -> str:
    return "".join(f"BT /F1 12 Tf 72 {top - 20 * i} Td ({esc(l)}) Tj ET\n" for i, l in enumerate(lines))


def table_content(rows: list[list[str]], top: int = 600, col_w: int = 100, row_h: int = 20) -> str:
    ncols, nrows = len(rows[0]), len(rows)
    left, bottom = 72, top - nrows * row_h
    out = []
    for r in range(nrows + 1):
        y = top - r * row_h
        out.append(f"{left} {y} m {left + ncols * col_w} {y} l S\n")
    for c in range(ncols + 1):
        x = left + c * col_w
        out.append(f"{x} {top} m {x} {bottom} l S\n")
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            out.append(f"BT /F1 10 Tf {left + c * col_w + 4} {top - r * row_h - 14} Td ({esc(cell)}) Tj ET\n")
    return "".join(out)


IMAGE_CONTENT = "q 400 0 0 400 50 300 cm /Im0 Do Q\n"


def build_pdf(pages: list[dict], encrypt_user_pw: str | None = None) -> bytes:
    """pages: [{"content": str, "image": bool}]. Objects: 1 catalog, 2 pages, 3 font, 4 image, then per page."""
    objs: dict[int, bytes] = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = []
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    hexdata = (b"80" * 64 * 64) + b">"
    objs[4] = (b"<< /Type /XObject /Subtype /Image /Width 64 /Height 64 /ColorSpace /DeviceGray "
               b"/BitsPerComponent 8 /Filter /ASCIIHexDecode /Length %d >>\nstream\n" % len(hexdata)
               + hexdata + b"\nendstream")
    n = 5
    for pg in pages:
        pid, cid = n, n + 1
        n += 2
        kids.append(f"{pid} 0 R")
        res = "<< /Font << /F1 3 0 R >>" + (" /XObject << /Im0 4 0 R >>" if pg.get("image") else "") + " >>"
        objs[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources {res} "
                     f"/Contents {cid} 0 R >>").encode()
        body = pg["content"].encode("latin-1")
        objs[cid] = b"<< /Length %d >>\nstream\n" % len(body) + body + b"endstream"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode() if num in offsets else b"0000000000 65535 f \n"
    trailer = f"/Size {size} /Root 1 0 R"
    if encrypt_user_pw is not None:
        o, pbytes, doc_id = bytes(range(32)), struct.pack("<i", -4), b"0123456789abcdef"
        padded = (encrypt_user_pw.encode("latin-1") + PAD)[:32]
        key = hashlib.md5(padded + o + pbytes + doc_id).digest()[:5]
        u = _rc4(key, PAD)
        trailer += (f" /Encrypt << /Filter /Standard /V 1 /R 2 /O <{o.hex()}> /U <{u.hex()}> /P -4 >>"
                    f" /ID [<{doc_id.hex()}> <{doc_id.hex()}>]")
    out += f"trailer\n<< {trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", png[16:24])


LONG = ["This page has plenty of ordinary text on it.", "Second line of ordinary text for the page."]


class TextAndTableTests(unittest.TestCase):
    def test_text_page_with_page_number(self):
        r = P.render_pdf(build_pdf([{"content": text_content(["ASTM A105 150# RF", "Buyer: Someone Else"])}]))
        self.assertTrue(r.ok, r.reason)
        self.assertIn("--- Page 1 of 1 ---", r.text)
        self.assertIn("ASTM A105 150# RF", r.text)
        self.assertEqual((r.text_pages, r.scanned_pages, r.total_pages), ([1], [], 1))
        self.assertIsNone(r.pages[0].png)

    def test_pages_numbered_in_order(self):
        r = P.render_pdf(build_pdf([{"content": text_content([f"{w} page text here, long enough"])} for w in ("alpha", "beta", "gamma")]))
        t = r.text
        self.assertLess(t.index("Page 1 of 3"), t.index("alpha"))
        self.assertLess(t.index("alpha"), t.index("Page 2 of 3"))
        self.assertLess(t.index("Page 2 of 3"), t.index("beta"))
        self.assertLess(t.index("beta"), t.index("Page 3 of 3"))
        self.assertLess(t.index("Page 3 of 3"), t.index("gamma"))

    def test_table_rows_with_pipes_and_text_not_printed_twice(self):
        rows = [["ITEM", "QTY", "DESC"], ["1", "12", "elbowzz"], ["2", "6", "teezz"]]
        content = text_content(["Heading above the table"], top=740) + table_content(rows, top=600) \
            + text_content(["Footer below the table"], top=400)
        r = P.render_pdf(build_pdf([{"content": content}]))
        t = r.text
        self.assertIn("[TABLE 1]", t)
        self.assertIn("R1: ITEM | QTY | DESC", t)
        self.assertIn("R2: 1 | 12 | elbowzz", t)
        self.assertIn("R3: 2 | 6 | teezz", t)
        self.assertEqual(t.count("elbowzz"), 1)        # table text only appears as a row
        self.assertEqual(t.count("teezz"), 1)
        self.assertLess(t.index("Heading above the table"), t.index("[TABLE 1]"))   # reading order kept
        self.assertLess(t.index("[TABLE 1]"), t.index("Footer below the table"))
        self.assertEqual(r.pages[0].tables, 1)


class ScanTests(unittest.TestCase):
    SCAN = {"content": IMAGE_CONTENT, "image": True}
    TEXT = {"content": text_content(LONG)}

    def test_scanned_page_flagged_and_rasterized_at_configured_dpi(self):
        r = P.render_pdf(build_pdf([self.SCAN]))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual((r.text_pages, r.scanned_pages), ([], [1]))
        pg = r.pages[0]
        self.assertEqual((pg.chars, pg.images), (0, 1))
        w, h = png_size(pg.png)
        self.assertLessEqual(abs(w - 612 * config.PDF_RASTER_DPI / 72), 1)   # renderer may round up a pixel
        self.assertLessEqual(abs(h - 792 * config.PDF_RASTER_DPI / 72), 1)
        self.assertIn("SCANNED", r.text)
        with mock.patch.object(config, "PDF_RASTER_DPI", 72):
            w, h = png_size(P.render_pdf(build_pdf([self.SCAN])).pages[0].png)
            self.assertLessEqual((abs(w - 612), abs(h - 792)), (1, 1))

    def test_mixed_document_reports_which_pages_are_which(self):
        r = P.render_pdf(build_pdf([self.TEXT, self.SCAN, self.TEXT]))
        self.assertEqual((r.text_pages, r.scanned_pages), ([1, 3], [2]))
        self.assertIsNone(r.pages[0].png)
        self.assertIsNotNone(r.pages[1].png)

    def test_text_threshold_is_configurable(self):
        short = {"content": text_content(["tiny 12345"])}   # 9 non-space characters
        self.assertEqual(P.render_pdf(build_pdf([short])).scanned_pages, [1])
        with mock.patch.object(config, "PDF_SCAN_MIN_CHARS", 5):
            r = P.render_pdf(build_pdf([short]))
        self.assertEqual((r.text_pages, r.scanned_pages), ([1], []))


class CapAndUnreadableTests(unittest.TestCase):
    def test_page_cap_returns_first_pages_with_reason(self):
        pdf = build_pdf([{"content": text_content([f"page {i} with enough ordinary text on it"])} for i in range(1, 6)])
        with mock.patch.object(config, "PDF_MAX_PAGES", 2):
            r = P.render_pdf(pdf)
        self.assertTrue(r.ok)
        self.assertTrue(r.truncated)
        self.assertEqual((len(r.pages), r.total_pages), (2, 5))
        self.assertIn("5 pages", r.truncation_reason)
        self.assertIn("only the first 2", r.truncation_reason)
        self.assertIn("[TRUNCATED:", r.text)
        self.assertNotIn("page 3", r.text)

    def test_default_cap_is_fifteen_and_under_cap_not_flagged(self):
        self.assertEqual(config.PDF_MAX_PAGES, 15)
        r = P.render_pdf(build_pdf([{"content": text_content(LONG)}]))
        self.assertFalse(r.truncated)
        self.assertIsNone(r.truncation_reason)

    def test_password_protected_reports_the_real_reason(self):
        r = P.render_pdf(build_pdf([{"content": text_content(LONG)}], encrypt_user_pw="secret"))
        self.assertEqual(r.status, "unreadable")
        self.assertIn("password", r.reason)
        self.assertEqual(r.pages, [])

    def test_unreadable_cases_never_raise(self):
        good = build_pdf([{"content": text_content(LONG)}])
        cases = {
            "empty": (b"", "empty"),
            "not a pdf": (b"just some text, not a pdf", "not a PDF"),
            "header only": (b"%PDF-1.4\n", "no readable pages|corrupt|cannot open"),
            "garbage after header": (b"%PDF-1.4\n" + b"\x00\xffgarbage" * 50, "corrupt|cannot open|no readable pages"),
            "cut off in the middle": (good[: len(good) // 3], "corrupt|cannot open|no readable pages"),
        }
        import re
        for name, (blob, pattern) in cases.items():
            with self.subTest(name):
                r = P.render_pdf(blob)
                self.assertEqual(r.status, "unreadable", r.text[:80])
                self.assertTrue(re.search(pattern, r.reason), r.reason)
                self.assertEqual(r.text, "")

    def test_missing_eof_is_warned_when_still_readable(self):
        good = build_pdf([{"content": text_content(LONG)}])
        r = P.render_pdf(good.replace(b"%%EOF\n", b""))
        if r.ok:
            self.assertTrue(any("%%EOF" in w for w in r.warnings))

    def test_oversized_file_refused(self):
        with mock.patch.object(P, "MAX_BYTES", 10):
            r = P.render_pdf(build_pdf([{"content": text_content(LONG)}]))
        self.assertEqual(r.status, "unreadable")
        self.assertIn("too large", r.reason)

    def test_non_bytes_input_does_not_raise(self):
        self.assertEqual(P.render_pdf(None).status, "unreadable")


class RealFileRegression(unittest.TestCase):
    """Three real dev attachments (not built here)."""

    EMAILS = Path(__file__).resolve().parent.parent / "data" / "dev" / "emails"

    def load(self, eid: str):
        from rfqparse.ingest import ingest_eml
        pdfs = [a for a in ingest_eml(self.EMAILS / f"{eid}.eml").attachments if (a.filename or "").lower().endswith(".pdf")]
        self.assertEqual(len(pdfs), 1)
        return P.render_pdf(pdfs[0].data)

    def test_e006_text_form_with_one_table_per_page(self):
        r = self.load("E006")
        self.assertEqual((r.text_pages, r.scanned_pages, r.total_pages), ([1, 2], [], 2))
        self.assertEqual([p.tables for p in r.pages], [1, 1])
        rows = [l for l in r.text.splitlines() if l.startswith("R") and " | " in l and "ITEM" not in l]
        self.assertEqual(len(rows), 42)
        self.assertLess(r.text.index("ASTM A105"), r.text.index("[TABLE 1]"))   # header block above the table

    def test_e009_is_all_scanned_with_png_per_page(self):
        r = self.load("E009")
        self.assertEqual((r.text_pages, r.scanned_pages), ([], [1, 2]))
        for pg in r.pages:
            png_size(pg.png)

    def test_e022_password_pdf_is_typed_unreadable(self):
        r = self.load("E022")
        self.assertEqual(r.status, "unreadable")
        self.assertIn("password", r.reason)


if __name__ == "__main__":
    unittest.main()
