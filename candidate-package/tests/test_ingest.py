"""Tests for rfqparse.ingest using synthetic emails built here (none are copies of dev data).

    python tests/test_ingest.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rfqparse.ingest import html_to_text, ingest_eml, split_quoted  # noqa: E402


def make(body="hello", html=None, frm="Pat Q <pat@acme.test>", subject="Need pricing",
         date="Mon, 02 Nov 2026 09:00:00 -0600", extra=None, attachments=()):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "rfq@ridgeline-supply.example", subject
    if date is not None:
        m["Date"] = date
    m["Message-ID"] = "<abc@acme.test>"
    for k, v in (extra or {}).items():
        m[k] = v
    if body is not None:
        m.set_content(body)
    if html is not None:
        m.add_alternative(html, subtype="html") if body is not None else m.set_content(html, subtype="html")
    for a in attachments:
        a(m)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, msg, name="S001"):
        p = Path(self.tmp.name) / f"{name}.eml"
        p.write_bytes(msg.as_bytes() if hasattr(msg, "as_bytes") else msg)
        return ingest_eml(p)


class HeaderTests(Base):
    def test_basic_fields(self):
        m = self.load(make(extra={"In-Reply-To": "<p@x>", "References": "<a@x> <b@x>"}))
        self.assertEqual((m.id, m.subject, m.message_id), ("S001", "Need pricing", "<abc@acme.test>"))
        self.assertEqual(m.sender_address, "pat@acme.test")
        self.assertEqual(m.in_reply_to, "<p@x>")
        self.assertEqual(m.references, ["<a@x>", "<b@x>"])
        self.assertIsNone(m.error)

    def test_date_aware_missing_broken(self):
        d = self.load(make()).date
        self.assertIsNotNone(d.tzinfo)
        self.assertEqual(d.utcoffset().total_seconds(), -6 * 3600)
        self.assertIsNone(self.load(make(date=None)).date)
        self.assertIsNone(self.load(make(date="last tuesday-ish")).date)

    def test_hints(self):
        m = self.load(make(extra={"List-Unsubscribe": "<mailto:u@x>", "Precedence": "bulk",
                                  "Auto-Submitted": "auto-replied"}))
        self.assertEqual(m.hints, {"list_unsubscribe": "<mailto:u@x>", "precedence": "bulk",
                                   "auto_submitted": "auto-replied"})
        self.assertEqual(set(self.load(make()).hints.values()), {None})

    def test_ridgeline_sender_is_exact_domain(self):
        self.assertTrue(self.load(make(frm="Rep <rep@ridgeline-supply.example>")).is_ridgeline_sender)
        self.assertFalse(self.load(make(frm="Rep <rep@ridgeline-supply.example.evil.test>")).is_ridgeline_sender)
        self.assertFalse(self.load(make(frm="MAILER-DAEMON <m@mx1.ridgeline-supply.example>")).is_ridgeline_sender)
        self.assertFalse(self.load(make()).is_ridgeline_sender)


class BodyTests(Base):
    def test_prefers_plain_over_html(self):
        m = self.load(make(body="plain version", html="<p>html version</p>"))
        self.assertEqual(m.text.strip(), "plain version")

    def test_html_only_becomes_text(self):
        m = self.load(make(body=None, html="<html><head><style>p{}</style></head><body><p>Two lines</p>"
                                           "<p>here &amp; there</p><table><tr><td>a</td><td>b</td></tr></table>"
                                           "<script>x()</script></body></html>"))
        self.assertIn("Two lines\nhere & there", m.text)
        self.assertIn("a | b", m.text)
        self.assertNotIn("x()", m.text)
        self.assertNotIn("p{}", m.text)

    def test_html_blockquote_becomes_quoted(self):
        t = html_to_text("<div>new stuff</div><blockquote><div>old stuff</div><div>more old</div></blockquote>")
        cur, quo, _ = split_quoted(t)
        self.assertEqual(cur, "new stuff")
        self.assertIn("old stuff", quo)


class SplitTests(Base):
    def test_wrote_marker(self):
        cur, quo, mk = split_quoted("Please add one.\n\nOn Mon, Nov 2, 2026 at 9:00 AM Sam <s@x.test> wrote:\n> old\n> older")
        self.assertEqual(cur, "Please add one.")
        self.assertTrue(quo.startswith("On Mon") and "> older" in quo)
        self.assertTrue(mk.startswith("wrote"))

    def test_wrote_marker_wrapped_over_lines(self):
        cur, quo, _ = split_quoted("Yes.\n\nOn Mon, Nov 2, 2026 at 9:00 AM Samantha Longname\n<s@x.test> wrote:\n\nold text")
        self.assertEqual(cur, "Yes.")
        self.assertIn("old text", quo)

    def test_original_message_marker(self):
        cur, quo, _ = split_quoted("Top note\n\n-----Original Message-----\nFrom: a\nold")
        self.assertEqual(cur, "Top note")
        self.assertTrue(quo.startswith("-----Original Message-----"))

    def test_from_sent_header_block(self):
        cur, quo, mk = split_quoted("Any news?\n\nFrom: Sam\nSent: Friday, November 6, 2026 10:15 AM\nTo: Desk\nSubject: RFQ\n\nold body")
        self.assertEqual(cur, "Any news?")
        self.assertIn("old body", quo)
        self.assertTrue(mk.startswith("header-block"))

    def test_angle_run(self):
        cur, quo, _ = split_quoted("Fine by me.\n> a\n> b\n> c")
        self.assertEqual((cur, quo), ("Fine by me.", "> a\n> b\n> c"))

    def test_no_marker_everything_current(self):
        cur, quo, mk = split_quoted("Line one\nOn the other hand, we want 5 elbows\nLine three")
        self.assertEqual((quo, mk), ("", None))
        self.assertIn("5 elbows", cur)

    def test_nothing_is_dropped(self):
        txt = "a\nOn Mon, Nov 2 at 9:00 Sam wrote:\n> b\nc"
        cur, quo, _ = split_quoted(txt)
        self.assertEqual((cur + "\n" + quo).split(), txt.split())

    def test_ridgeline_sender_forward_is_not_quoted(self):
        body = "Please quote.\n\n-----Original Message-----\nFrom: Cust <c@cust.test>\nSent: Monday\nTo: Rep\nSubject: list\n\n10 ea - 2in elbow"
        m = self.load(make(body=body, frm="Rep <rep@ridgeline-supply.example>"))
        self.assertEqual(m.quoted_text, "")
        self.assertIn("10 ea - 2in elbow", m.current_text)
        m2 = self.load(make(body=body, frm="Cust <c@cust.test>"))
        self.assertNotIn("10 ea", m2.current_text)
        self.assertIn("10 ea", m2.quoted_text)


class AttachmentTests(Base):
    def test_attachments_kept_with_metadata(self):
        def csv(m): m.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="list.csv")
        def png(m): m.add_attachment(b"\x89PNGfake", maintype="image", subtype="png", filename="pic.png",
                                     disposition="inline", cid="<img1@x>")
        m = self.load(make(attachments=[csv, png]))
        by = {a.filename: a for a in m.attachments}
        self.assertEqual(by["list.csv"].data, b"a,b\n1,2\n")
        self.assertEqual(by["list.csv"].content_type, "text/csv")
        self.assertFalse(by["list.csv"].inline)
        self.assertTrue(by["pic.png"].inline)
        self.assertEqual(by["pic.png"].content_id, "img1@x")
        self.assertNotIn("a,b", m.text)

    def test_rfc822_attachment_stays_an_attachment(self):
        inner = make(body="inner request: 99 ea widgets", frm="Rep <rep@ridgeline-supply.example>")
        def att(m): m.add_attachment(inner)
        m = self.load(make(body="Delivery failed", attachments=[att]))
        self.assertEqual([a.content_type for a in m.attachments], ["message/rfc822"])
        self.assertIn(b"99 ea widgets", m.attachments[0].data)
        self.assertNotIn("99 ea", m.text)


class RobustnessTests(Base):
    def test_missing_file_never_raises(self):
        m = ingest_eml(Path(self.tmp.name) / "nope.eml")
        self.assertEqual(m.id, "nope")
        self.assertIn("cannot read", m.error)

    def test_garbage_bytes_never_raise(self):
        for blob in (b"", b"\x00\xff\xfe garbage", b"Subject: x\n\n\xff\xfe\xfd"):
            m = self.load(blob)
            self.assertEqual(m.id, "S001")

    def test_bad_charset_and_encoding_does_not_raise(self):
        raw = (b"From: a@b.test\nSubject: s\nContent-Type: text/plain; charset=nonsense-9\n"
               b"Content-Transfer-Encoding: base64\n\n!!!notbase64!!!\n")
        m = self.load(raw)
        self.assertIsNone(m.error)


if __name__ == "__main__":
    unittest.main()
