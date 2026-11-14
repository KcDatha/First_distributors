"""Tests for rfqparse.render.docx with .docx files built here by hand (zip + XML).

    python tests/test_render_docx.py -v
"""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rfqparse.render import docx as D  # noqa: E402

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make(body: str, styles: str | None = None, extra: dict[str, str] | None = None) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>')
        if styles:
            z.writestr("word/styles.xml", f'<?xml version="1.0"?><w:styles {NS}>{styles}</w:styles>')
        for name, xml in (extra or {}).items():
            root = name.split("/")[-1].rstrip("0123456789.xml")
            tag = {"header": "hdr", "footer": "ftr", "footnotes": "footnotes", "endnotes": "endnotes"}[root]
            z.writestr(name, f'<?xml version="1.0"?><w:{tag} {NS}>{xml}</w:{tag}>')
    return b.getvalue()


def p(text: str, style: str | None = None, extra_ppr: str = "", rpr: str = "") -> str:
    ppr = ""
    if style or extra_ppr:
        ppr = f"<w:pPr>{f'<w:pStyle w:val={chr(34)}{style}{chr(34)}/>' if style else ''}{extra_ppr}</w:pPr>"
    return f"<w:p>{ppr}<w:r>{f'<w:rPr>{rpr}</w:rPr>' if rpr else ''}<w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"


def tc(content: str, tcpr: str = "") -> str:
    return f"<w:tc>{f'<w:tcPr>{tcpr}</w:tcPr>' if tcpr else ''}{content}</w:tc>"


def tr(*cells: str) -> str:
    return f"<w:tr>{''.join(cells)}</w:tr>"


def tbl(*rows: str) -> str:
    return f"<w:tbl>{''.join(rows)}</w:tbl>"


class BodyTests(unittest.TestCase):
    def test_document_order_paragraphs_and_tables(self):
        body = (p("before the table") + tbl(tr(tc(p("a")), tc(p("b"))), tr(tc(p("c")), tc(p("d"))))
                + p("between") + tbl(tr(tc(p("x")), tc(p("y")))) + p("after everything"))
        t = D.render_docx(make(body)).text
        order = [t.index(s) for s in ("before the table", "R1: a | b", "R2: c | d", "between",
                                      "R1: x | y", "after everything")]
        self.assertEqual(order, sorted(order))
        self.assertIn("[TABLE 1]", t)
        self.assertIn("[TABLE 2]", t)

    def test_heading_by_style_name_even_with_custom_id(self):
        styles = ('<w:style w:styleId="Ttl1"><w:name w:val="heading 1"/></w:style>'
                  '<w:style w:styleId="Mine"><w:name w:val="My Head"/><w:basedOn w:val="Ttl1"/></w:style>'
                  '<w:style w:styleId="Normal"><w:name w:val="Normal"/></w:style>')
        t = D.render_docx(make(p("Big heading", "Ttl1") + p("Derived heading", "Mine") + p("plain", "Normal"), styles)).text
        self.assertIn("[HEADING 1] Big heading", t)
        self.assertIn("[HEADING 1] Derived heading", t)
        self.assertIn("\nplain", t)
        self.assertNotIn("[HEADING 1] plain", t)

    def test_heading_by_id_without_styles_part_and_by_outline_level(self):
        t = D.render_docx(make(p("From id", "Heading2") + p("From outline", extra_ppr='<w:outlineLvl w:val="2"/>'))).text
        self.assertIn("[HEADING 2] From id", t)
        self.assertIn("[HEADING 3] From outline", t)

    def test_title_style(self):
        styles = '<w:style w:styleId="Title"><w:name w:val="Title"/></w:style>'
        self.assertIn("[TITLE] My Doc", D.render_docx(make(p("My Doc", "Title"), styles)).text)

    def test_table_cells_joined_with_pipes_and_multi_paragraph_cell(self):
        body = tbl(tr(tc(p("one") + p("two")), tc(p("three"))))
        self.assertIn("R1: one / two | three", D.render_docx(make(body)).text)

    def test_list_items(self):
        num = '<w:numPr><w:ilvl w:val="{l}"/><w:numId w:val="1"/></w:numPr>'
        body = p("first", extra_ppr=num.format(l=0)) + p("nested", extra_ppr=num.format(l=1)) + p("not a list")
        t = D.render_docx(make(body)).text
        self.assertIn("\n- first", t)
        self.assertIn("\n  - nested", t)
        self.assertIn("\nnot a list", t)

    def test_numid_zero_is_not_a_list(self):
        body = p("plain", extra_ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="0"/></w:numPr>')
        self.assertNotIn("- plain", D.render_docx(make(body)).text)

    def test_content_control_and_hyperlink_text_kept(self):
        body = ('<w:sdt><w:sdtContent>' + p("inside a content control") + '</w:sdtContent></w:sdt>'
                '<w:p><w:hyperlink><w:r><w:t>linked words</w:t></w:r></w:hyperlink></w:p>')
        t = D.render_docx(make(body)).text
        self.assertIn("inside a content control", t)
        self.assertIn("linked words", t)


class StrikeAndTrackedChangeTests(unittest.TestCase):
    def test_struck_run(self):
        t = D.render_docx(make(p("keep") + p("old line", rpr="<w:strike/>"))).text
        self.assertIn("\nkeep", t)
        self.assertIn("[STRUCK: old line]", t)

    def test_strike_val_false_is_not_struck(self):
        t = D.render_docx(make(p("not struck", rpr='<w:strike w:val="0"/>'))).text
        self.assertIn("\nnot struck", t)
        self.assertNotIn("[STRUCK: not struck]", t)

    def test_partial_strike_inside_paragraph(self):
        body = ('<w:p><w:r><w:t xml:space="preserve">want </w:t></w:r>'
                '<w:r><w:rPr><w:strike/></w:rPr><w:t xml:space="preserve">12</w:t></w:r>'
                '<w:r><w:t xml:space="preserve"> 14 ea</w:t></w:r></w:p>')
        self.assertIn("want [STRUCK: 12] 14 ea", D.render_docx(make(body)).text)

    def test_tracked_deletion_is_struck_and_insertion_is_normal(self):
        body = ('<w:p><w:r><w:t xml:space="preserve">qty </w:t></w:r>'
                '<w:del><w:r><w:delText>10</w:delText></w:r></w:del>'
                '<w:ins><w:r><w:t>14</w:t></w:r></w:ins></w:p>')
        t = D.render_docx(make(body)).text
        self.assertIn("qty [STRUCK: 10]14", t)
        self.assertNotIn("STRUCK: 14", t)

    def test_struck_text_in_table_cell(self):
        body = tbl(tr(tc(p("kept")), tc(p("dropped", rpr="<w:strike/>"))))
        self.assertIn("R1: kept | [STRUCK: dropped]", D.render_docx(make(body)).text)


class MergeTests(unittest.TestCase):
    def test_vertical_merge_carries_value_down(self):
        body = tbl(tr(tc(p("PIPE"), '<w:vMerge w:val="restart"/>'), tc(p("2in"))),
                   tr(tc(p(""), "<w:vMerge/>"), tc(p("3in"))),
                   tr(tc(p("other")), tc(p("4in"))))
        t = D.render_docx(make(body)).text
        self.assertIn("R1: PIPE | 2in", t)
        self.assertIn("R2: [merged] PIPE | 3in", t)
        self.assertIn("R3: other | 4in", t)

    def test_horizontal_span_value_once_columns_kept(self):
        body = tbl(tr(tc(p("WIDE HEADING"), '<w:gridSpan w:val="3"/>'), tc(p("end"))))
        t = D.render_docx(make(body)).text
        self.assertEqual(t.count("WIDE HEADING"), 1)
        self.assertIn("R1: WIDE HEADING |  |  | end", t)


class TextBoxTests(unittest.TestCase):
    CHOICE = ('<w:p><w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
              '<mc:Choice><w:drawing><w:txbxContent>{inner}</w:txbxContent></w:drawing></mc:Choice>'
              '<mc:Fallback><w:pict><w:txbxContent>{inner}</w:txbxContent></w:pict></mc:Fallback>'
              '</mc:AlternateContent></w:r></w:p>')

    def test_text_box_read_labelled_and_not_duplicated(self):
        body = p("before") + self.CHOICE.format(inner=p("ALL FLANGES 150# RF")) + p("after")
        t = D.render_docx(make(body)).text
        self.assertEqual(t.count("ALL FLANGES 150# RF"), 1)       # Choice copy only, Fallback ignored
        self.assertIn("[TEXT BOX] ALL FLANGES 150# RF", t)
        self.assertLess(t.index("\nbefore"), t.index("\n[TEXT BOX]"))   # (the legend also names [TEXT BOX])
        self.assertLess(t.index("\n[TEXT BOX]"), t.index("\nafter"))

    def test_text_box_with_table_and_struck_text(self):
        inner = p("note", rpr="<w:strike/>") + tbl(tr(tc(p("a")), tc(p("b"))))
        t = D.render_docx(make(self.CHOICE.format(inner=inner))).text
        self.assertIn("[TEXT BOX] [STRUCK: note]", t)
        self.assertIn("[TEXT BOX] R1: a | b", t)

    def test_text_box_in_header(self):
        t = D.render_docx(make(p("body"), extra={"word/header1.xml": self.CHOICE.format(inner=p("HDR BOX"))})).text
        self.assertIn("[TEXT BOX] HDR BOX", t)


class RealFileRegression(unittest.TestCase):
    """Renders the real dev E019 attachment (not a file built here)."""

    EML = Path(__file__).resolve().parent.parent / "data" / "dev" / "emails" / "E019.eml"

    def test_e019_headings_in_order_and_table_row_counts(self):
        from rfqparse.ingest import ingest_eml
        mail = ingest_eml(self.EML)
        docs = [a for a in mail.attachments if (a.filename or "").lower().endswith(".docx")]
        self.assertEqual(len(docs), 1)
        r = D.render_docx(docs[0].data)
        self.assertTrue(r.ok, r.reason)
        lines = r.text.splitlines()
        headings = [i for i, l in enumerate(lines) if l.startswith("[HEADING")]
        self.assertEqual(len(headings), 2)
        self.assertIn("SCH 80", lines[headings[0]])
        self.assertIn("3000#", lines[headings[1]])
        tables = [i for i, l in enumerate(lines) if l.startswith("[TABLE")]
        self.assertEqual(len(tables), 2)
        # heading, then its table, then the next heading, then its table: strict order
        self.assertLess(headings[0], tables[0])
        self.assertLess(tables[0], headings[1])
        self.assertLess(headings[1], tables[1])
        bounds = [tables[0], headings[1], tables[1], len(lines)]
        data_rows = []
        for start, end in ((bounds[0], bounds[1]), (bounds[2], bounds[3])):
            rows = [l for l in lines[start + 1:end] if l.startswith("R") and ": " in l]
            data_rows.append(len(rows) - 1)  # minus the header row R1
        self.assertEqual(data_rows, [4, 5])


class EdgeTests(unittest.TestCase):
    def test_headers_footers_footnotes_included_and_labelled(self):
        t = D.render_docx(make(p("body text"), extra={
            "word/header1.xml": p("ALL ITEMS SCH 80 UNLESS NOTED"),
            "word/footer1.xml": p("Prices valid 30 days"),
            "word/footnotes.xml": p("footnote: see drawing 7"),
        })).text
        self.assertIn("=== HEADER (header1.xml) ===\nALL ITEMS SCH 80 UNLESS NOTED", t)
        self.assertIn("=== FOOTER (footer1.xml) ===\nPrices valid 30 days", t)
        self.assertIn("=== FOOTNOTES (footnotes.xml) ===\nfootnote: see drawing 7", t)
        self.assertLess(t.index("HEADER"), t.index("=== BODY ==="))
        self.assertLess(t.index("=== BODY ==="), t.index("FOOTER"))

    def test_identical_headers_listed_once(self):
        t = D.render_docx(make(p("b"), extra={"word/header1.xml": p("same"), "word/header2.xml": p("same")})).text
        self.assertEqual(t.count("same"), 1)

    def test_body_cap_says_so_and_keeps_footer(self):
        body = "".join(p(f"line number {i} " + "x" * 80) for i in range(100))
        with mock.patch.object(D, "MAX_TOTAL_CHARS", 4000):
            r = D.render_docx(make(body, extra={"word/footer1.xml": p("FOOTER SURVIVES")}))
        self.assertTrue(r.truncated)
        self.assertIn("[TRUNCATED: document body cut", r.text)
        self.assertIn("FOOTER SURVIVES", r.text)
        self.assertNotIn("line number 99", r.text)

    def test_untruncated_not_flagged(self):
        self.assertFalse(D.render_docx(make(p("small"))).truncated)

    def test_unreadable_cases_never_raise(self):
        good = make(p("hello"))
        notdoc = io.BytesIO()
        with zipfile.ZipFile(notdoc, "w") as z:
            z.writestr("hello.txt", "hi")
        badxml = io.BytesIO()
        with zipfile.ZipFile(badxml, "w") as z:
            z.writestr("word/document.xml", "<w:document><unclosed>")
        dtd = io.BytesIO()
        with zipfile.ZipFile(dtd, "w") as z:
            z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><d>&a;</d>')
        cases = {
            "empty": b"",
            "garbage": b"definitely not a word file",
            "truncated zip": good[: len(good) // 2],
            "zip without document.xml": notdoc.getvalue(),
            "invalid xml": badxml.getvalue(),
            "DTD/entities": dtd.getvalue(),
            "legacy/encrypted OLE": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
            "no text at all": make(""),
        }
        for name, blob in cases.items():
            with self.subTest(name):
                r = D.render_docx(blob)
                self.assertEqual(r.status, "unreadable")
                self.assertTrue(r.reason)
                self.assertEqual(r.text, "")

    def test_non_bytes_does_not_raise(self):
        self.assertEqual(D.render_docx(None).status, "unreadable")

    def test_bad_header_part_is_flagged_but_body_still_renders(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("word/document.xml", f'<w:document {NS}><w:body>{p("body survives")}</w:body></w:document>')
            z.writestr("word/header1.xml", "<w:hdr><broken")
        r = D.render_docx(b.getvalue())
        self.assertTrue(r.ok)
        self.assertIn("body survives", r.text)
        self.assertIn("UNREADABLE", r.text)
        self.assertTrue(any("header1.xml" in w for w in r.warnings))


if __name__ == "__main__":
    unittest.main()
