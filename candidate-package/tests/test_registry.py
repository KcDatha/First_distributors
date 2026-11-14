"""Tests for rfqparse.render.registry using emails and attachments built here.

    python tests/test_registry.py -v
"""

from __future__ import annotations

import base64
import io
import sys
import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from openpyxl import Workbook
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from rfqparse.render import registry as R  # noqa: E402
from test_render_pdf import IMAGE_CONTENT, LONG, build_pdf, text_content  # noqa: E402


def png(w=120, h=80, color="white") -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def xlsx_bytes(text="widget") -> bytes:
    wb = Workbook()
    wb.active["A1"] = text
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def docx_bytes(text="hello") -> bytes:
    b = io.BytesIO()
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", f'<w:document {ns}><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return b.getvalue()


def with_media(zip_bytes: bytes, media: dict[str, bytes]) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(zip_bytes))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n in src.namelist():
            z.writestr(n, src.read(n))
        for n, d in media.items():
            z.writestr(n, d)
    return out.getvalue()


def make_mail(attachments=(), body="please quote", html=None, frm="Pat <pat@acme.test>") -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "rfq@ridgeline-supply.example", "Need pricing"
    m["Date"] = "Mon, 02 Nov 2026 09:00:00 -0600"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    for data, mt, st, name, kw in attachments:
        m.add_attachment(data, maintype=mt, subtype=st, filename=name, **kw)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def render(self, msg, name="S001"):
        p = Path(self.tmp.name) / f"{name}.eml"
        p.write_bytes(msg.as_bytes() if hasattr(msg, "as_bytes") else msg)
        return R.render_path(p)

    def one(self, data, name, mt="application/octet-stream", **kw):
        maintype, subtype = mt.split("/")
        return self.render(make_mail([(data, maintype, subtype, name, kw)])).attachments


class MislabelTests(Base):
    def test_pdf_named_xlsx_is_read_as_pdf_and_the_mismatch_is_noted(self):
        pdf = build_pdf([{"content": text_content(LONG)}])
        a = self.one(pdf, "quote.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")[0]
        self.assertEqual((a.detected, a.status), ("pdf", "ok"))
        self.assertIn("ordinary text", a.text)
        self.assertIn("labelled as xlsx", a.note)
        self.assertIn("content is pdf", a.note)

    def test_other_mislabels_follow_the_bytes(self):
        cases = [
            ("xlsx content named .pdf", xlsx_bytes("zzz"), "list.pdf", "application/pdf", "xlsx", "zzz"),
            ("docx content named .xlsx", docx_bytes("docword"), "x.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "docx", "docword"),
            ("png named .pdf", png(), "scan.pdf", "application/pdf", "image", None),
            ("csv named .png", b"qty,item\n1,tee\n2,elbow\n", "pic.png", "image/png", "text", "qty"),
        ]
        for name, data, fname, mt, kind, snippet in cases:
            with self.subTest(name):
                a = self.one(data, fname, mt)[0]
                self.assertEqual(a.detected, kind)
                self.assertEqual(a.status, "ok", a.reason)
                if snippet:
                    self.assertIn(snippet, a.text)
                if kind == "image":
                    self.assertEqual(len(a.images), 1)

    def test_correct_labels_produce_no_note(self):
        a = self.one(xlsx_bytes(), "ok.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")[0]
        self.assertIsNone(a.note)

    def test_damaged_xlsx_is_routed_to_the_xlsx_handler_to_explain_why(self):
        data = xlsx_bytes()
        a = self.one(data[: len(data) // 2], "bom.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")[0]
        self.assertEqual((a.detected, a.status), ("xlsx", "unreadable"))
        self.assertRegex(a.reason, "corrupt|truncated|cannot open")


class UnreadableTypeTests(Base):
    def test_types_we_cannot_read_say_why(self):
        cases = {
            "old xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "old.xls", "legacy Office"),
            "plain zip": (_zip({"a.txt": b"hi"}), "files.zip", "zip archive"),
            "powerpoint": (_zip({"ppt/presentation.xml": b"<x/>"}), "deck.pptx", "PowerPoint"),
            "heic photo": (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64, "IMG_1.heic", "HEIC"),
            "unknown binary": (bytes(range(256)) * 4, "blob.bin", "unrecognized file type"),
        }
        for name, (data, fname, why) in cases.items():
            with self.subTest(name):
                a = self.one(data, fname)[0]
                self.assertEqual(a.status, "unreadable")
                self.assertIn(why, a.reason)
                self.assertEqual(a.text, "")

    def test_rfc822_attachment_stays_a_plain_attachment(self):
        inner = make_mail(body="inner request: 99 ea widgets")
        a = self.render(_with_attached_message(inner)).attachments[0]
        self.assertEqual((a.detected, a.status), ("email", "attached"))
        self.assertEqual(a.text, "")
        self.assertIn("not unpacked", a.reason)
        self.assertEqual(a.images, [])


def _zip(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


def _with_attached_message(inner: EmailMessage) -> EmailMessage:
    m = make_mail(body="Delivery failed")
    m.add_attachment(inner)
    return m


class RendererThrowsTests(Base):
    def test_a_renderer_that_raises_becomes_unreadable_and_others_still_render(self):
        mail = make_mail([
            (xlsx_bytes("sheetdata"), "application", "octet-stream", "a.xlsx", {}),
            (build_pdf([{"content": text_content(LONG)}]), "application", "pdf", "b.pdf", {}),
        ])
        with mock.patch.object(R, "render_xlsx", side_effect=RuntimeError("boom")):
            pkg = self.render(mail)
        a, b = pkg.attachments
        self.assertEqual(a.status, "unreadable")
        self.assertIn("boom", a.reason)
        self.assertIn("RuntimeError", a.reason)
        self.assertEqual(b.status, "ok")
        self.assertIn("ordinary text", b.text)
        self.assertIsNone(pkg.error)

    def test_each_handler_is_guarded(self):
        data = {
            "render_pdf": (build_pdf([{"content": text_content(LONG)}]), "x.pdf"),
            "render_docx": (docx_bytes(), "x.docx"),
            "render_csv": (b"a,b\n1,2\n", "x.csv"),
            "prepare_image": (png(), "x.png"),
        }
        for fn, (blob, name) in data.items():
            with self.subTest(fn), mock.patch.object(R, fn, side_effect=ValueError("kaboom")):
                a = self.one(blob, name)[0]
                self.assertEqual(a.status, "unreadable")
                self.assertIn("kaboom", a.reason)

    def test_detection_itself_failing_is_contained(self):
        with mock.patch.object(R, "detect_kind", side_effect=RuntimeError("detect broke")):
            a = self.one(png(), "x.png")[0]
        self.assertEqual(a.status, "unreadable")
        self.assertIn("detect broke", a.reason)

    def test_missing_file_gives_a_package_with_an_error(self):
        pkg = R.render_path(Path(self.tmp.name) / "nope.eml")
        self.assertEqual(pkg.id, "nope")
        self.assertTrue(pkg.error)


class EmbeddedPictureTests(Base):
    def test_xlsx_pictures_are_extracted_named_after_the_parent_and_announced(self):
        data = with_media(xlsx_bytes("sheet text"), {"xl/media/image1.png": png(200, 100), "xl/media/image2.png": png(50, 50, "red")})
        atts = self.one(data, "Tank.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        parent, kids = atts[0], atts[1:]
        self.assertEqual(len(kids), 2)
        self.assertTrue(all(k.parent == "Tank.xlsx" and k.source == "Tank.xlsx" for k in kids))
        self.assertEqual([k.note for k in kids], ["embedded image xl/media/image1.png", "embedded image xl/media/image2.png"])
        self.assertTrue(all(k.status == "ok" and len(k.images) == 1 for k in kids))
        self.assertIn("2 embedded picture(s)", parent.text)       # said in the output, not lost silently
        self.assertTrue(any("embedded picture" in w for w in parent.warnings))

    def test_docx_pictures_and_oddities_are_all_accounted_for(self):
        data = with_media(docx_bytes("doc text"), {
            "word/media/image1.png": png(),
            "word/media/image2.gif": _tiny_gif(),
            "word/media/image3.emf": b"\x01\x00\x00\x00 fake emf data " * 4,
        })
        atts = self.one(data, "Spec.docx")
        kids = atts[1:]
        self.assertEqual(len(kids), 3)                            # nothing dropped
        by = {k.note.split()[-1]: k for k in kids}
        self.assertEqual(by["word/media/image1.png"].status, "ok")
        self.assertEqual(by["word/media/image2.gif"].status, "skipped")           # tracking-pixel size, with a reason
        self.assertIn("IMAGE SKIPPED", by["word/media/image2.gif"].text)
        self.assertEqual(by["word/media/image3.emf"].status, "unreadable")       # unsupported, still listed
        self.assertIn("3 embedded picture(s)", atts[0].text)

    def test_no_media_no_message(self):
        a = self.one(xlsx_bytes(), "plain.xlsx")
        self.assertEqual(len(a), 1)
        self.assertNotIn("embedded", a[0].text)

    def test_picture_cap_is_reported(self):
        media = {f"word/media/image{i:02d}.png": png(40 + i, 40) for i in range(5)}
        with mock.patch.object(R, "MAX_EMBEDDED", 3):
            atts = self.one(with_media(docx_bytes(), media), "Many.docx")
        self.assertEqual(len(atts) - 1, 3)
        self.assertIn("2 more not taken", atts[0].text)


def _tiny_gif() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(b, "GIF")
    return b.getvalue()


class InlineImageTests(Base):
    def test_cid_referenced_image_counts_as_an_attachment_and_is_flagged(self):
        m = make_mail(html='<p>see <img src="cid:pic1@x"></p>')
        m.get_payload()[1].add_related(png(), "image", "png", cid="<pic1@x>", filename="pasted.png", disposition="inline")
        pkg = self.render(m)
        pics = [a for a in pkg.attachments if a.detected == "image"]
        self.assertEqual(len(pics), 1)
        self.assertTrue(pics[0].referenced_by_cid)
        self.assertTrue(pics[0].inline)
        self.assertEqual(pkg.image_count, 1)

    def test_dangling_cid_is_noted(self):
        pkg = self.render(make_mail(html='<img src="cid:gone@x">'))
        self.assertTrue(any("cid:gone@x" in n for n in pkg.notes))

    def test_data_uri_image_in_html_becomes_an_attachment(self):
        uri = "data:image/png;base64," + base64.b64encode(png(90, 60)).decode()
        pkg = self.render(make_mail(html=f'<img src="{uri}">'))
        pics = [a for a in pkg.attachments if a.detected == "image"]
        self.assertEqual(len(pics), 1)
        self.assertEqual(pics[0].source, "html-inline-image-1")
        self.assertEqual(pkg.image_count, 1)


class PackageTests(Base):
    def test_package_has_current_and_labeled_quoted_text_and_pdf_pages(self):
        body = "Add one more.\n\nOn Mon, Nov 2, 2026 at 9:00 AM Sam <s@x.test> wrote:\n> 10 ea elbows"
        scan = build_pdf([{"content": text_content(LONG)}, {"content": IMAGE_CONTENT, "image": True}])
        pkg = self.render(make_mail([(scan, "application", "pdf", "two.pdf", {})], body=body))
        self.assertEqual(pkg.current_text, "Add one more.")
        self.assertIn("10 ea elbows", pkg.quoted_text)
        self.assertTrue(pkg.quoted_labeled.startswith("[QUOTED EARLIER TEXT"))
        a = pkg.attachments[0]
        self.assertEqual((a.pdf.text_pages, a.pdf.scanned_pages), ([1], [2]))
        self.assertEqual([i.label for i in a.images], ["page 2 (scanned)"])
        out = R.format_package(pkg)
        self.assertIn("CURRENT MESSAGE TEXT", out)
        self.assertIn("QUOTED EARLIER TEXT", out)
        self.assertIn("[IMAGE for vision: page 2 (scanned):", out)
        self.assertIn("Subject: Need pricing", out)

    def test_summary_line_names_attachments_status_text_and_images(self):
        line = R.summary_line(self.render(make_mail([(png(), "image", "png", "p.png", {})])))
        self.assertIn("p.png[image]=ok", line)
        self.assertIn("images: 1", line)
        self.assertIn("S001", line)

    def test_unreadable_attachment_is_shown_in_the_debug_text_with_its_reason(self):
        out = R.format_package(self.render(make_mail([(b"\xd0\xcf\x11\xe0" + b"\x00" * 40, "application", "vnd.ms-excel", "old.xls", {})])))
        self.assertIn("status: unreadable", out)
        self.assertIn("REASON: legacy Office file", out)


if __name__ == "__main__":
    unittest.main()
