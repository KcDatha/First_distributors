"""Tests for rfqparse.postprocess.dupes and its use in the pipeline, with synthetic emails built here.

    python tests/test_dupes.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from rfqparse import pipeline as PL  # noqa: E402
from rfqparse.llm import cache as C  # noqa: E402
from rfqparse.llm import ledger as L  # noqa: E402
from rfqparse.llm.call import CallStats  # noqa: E402
from rfqparse.postprocess import dupes as D  # noqa: E402
from test_pipeline import FakeClient, answer, line  # noqa: E402
from test_registry import png, xlsx_bytes  # noqa: E402

BODY = "Please quote the attached list for the plant shutdown, we need it back by the end of the week."
SHEET_A = xlsx_bytes("list A")
SHEET_B = xlsx_bytes("list B, different contents")


def mail(frm="Pat Q <pat@acme.test>", subject="Shutdown list", date="Mon, 02 Nov 2026 09:00:00 -0600", body=BODY,
         attachments=(), inline=()):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "rfq@ridgeline-supply.example", subject
    if date is not None:
        m["Date"] = date
    m.set_content(body)
    for data, name in attachments:
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    for data, name in inline:
        m.add_attachment(data, maintype="image", subtype="png", filename=name, disposition="inline", cid=f"<{name}@x>")
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.emails = self.root / "emails"
        self.emails.mkdir()

    def put(self, eid, msg):
        (self.emails / f"{eid}.eml").write_bytes(msg.as_bytes())

    def find(self):
        return D.find_duplicates(PL._ingest_all(self.emails))

    def pair(self, first, second, ids=("A1", "A2")):
        self.put(ids[0], first)
        self.put(ids[1], second)
        return self.find()


T1, T2 = "Mon, 02 Nov 2026 09:00:00 -0600", "Tue, 03 Nov 2026 08:00:00 -0600"


class AttachmentRuleTests(Base):
    def test_resend_with_a_resend_prefix(self):
        d = self.pair(mail(subject="Shutdown list", date=T1, attachments=[(SHEET_A, "list.xlsx")]),
                      mail(subject="RESEND: Shutdown list", date=T2, attachments=[(SHEET_A, "list.xlsx")]))
        self.assertEqual(list(d), ["A2"])
        self.assertEqual(d["A2"].original, "A1")
        self.assertIn("sha256", d["A2"].reason)

    def test_resend_with_a_different_subject_and_even_a_renamed_file_is_still_the_same_request(self):
        d = self.pair(mail(subject="Shutdown list", date=T1, attachments=[(SHEET_A, "list.xlsx")]),
                      mail(subject="completely different words", date=T2, attachments=[(SHEET_A, "renamed copy.xlsx")]))
        self.assertEqual(d["A2"].original, "A1")

    def test_same_sender_different_attachment_is_not_a_duplicate(self):
        self.assertEqual(self.pair(mail(date=T1, attachments=[(SHEET_A, "l.xlsx")]),
                                   mail(date=T2, attachments=[(SHEET_B, "l.xlsx")])), {})

    def test_same_attachment_from_a_different_sender_is_not_a_duplicate(self):
        self.assertEqual(self.pair(mail(date=T1, attachments=[(SHEET_A, "l.xlsx")]),
                                   mail(frm="Other <other@acme.test>", date=T2, attachments=[(SHEET_A, "l.xlsx")])), {})

    def test_the_same_files_but_not_the_whole_set_is_not_a_duplicate(self):
        self.assertEqual(self.pair(mail(date=T1, attachments=[(SHEET_A, "a.xlsx")]),
                                   mail(date=T2, attachments=[(SHEET_A, "a.xlsx"), (SHEET_B, "b.xlsx")])), {})

    def test_a_changed_number_in_the_body_is_not_a_resend(self):
        d = self.pair(mail(date=T1, body="Please quote the list, need 10 of each, thanks a lot for the quick help.",
                           attachments=[(SHEET_A, "l.xlsx")]),
                      mail(date=T2, body="Please quote the list, need 12 of each, thanks a lot for the quick help.",
                           attachments=[(SHEET_A, "l.xlsx")]))
        self.assertEqual(d, {})

    def test_different_wording_with_the_same_numbers_is_a_resend(self):
        d = self.pair(mail(date=T1, body="Quote this: 2 sets of 4in gaskets and 6 sets of 6in gaskets.", attachments=[(SHEET_A, "l.xlsx")]),
                      mail(date=T2, body="Resending in case it was missed. 2 sets of 4in gaskets, 6 sets of 6in gaskets.",
                           attachments=[(SHEET_A, "l.xlsx")]))
        self.assertEqual(list(d), ["A2"])

    def test_signature_logos_are_ignored_for_the_attachment_rule_but_never_count_as_the_attachment(self):
        logo = png(200, 50, "navy")
        # same logo on two DIFFERENT requests: no real attachment, bodies differ: not duplicates
        self.assertEqual(self.pair(mail(date=T1, body=BODY + " One.", inline=[(logo, "logo.png")]),
                                   mail(date=T2, body=BODY + " Two, a different job.", inline=[(logo, "logo.png")])), {})
        # a different logo on a real resend: the attachment is what matters
        d = self.pair(mail(date=T1, attachments=[(SHEET_A, "l.xlsx")], inline=[(png(200, 50, "navy"), "logo.png")]),
                      mail(date=T2, attachments=[(SHEET_A, "l.xlsx")], inline=[(png(200, 50, "red"), "logo.png")]))
        self.assertEqual(list(d), ["A2"])


class BodyRuleTests(Base):
    def test_identical_body_and_subject_with_no_attachments_is_a_duplicate_ignoring_case_whitespace_and_prefixes(self):
        d = self.pair(mail(subject="Shutdown list", date=T1, body=BODY),
                      mail(subject="RE: Fwd: shutdown   LIST", date=T2, body=BODY.upper().replace(" ", "  ") + "\n\n"))
        self.assertEqual(list(d), ["A2"])
        self.assertIn("identical body", d["A2"].reason)

    def test_a_different_body_is_not_a_duplicate(self):
        self.assertEqual(self.pair(mail(date=T1, body=BODY), mail(date=T2, body=BODY + " Also add two more.")), {})

    def test_a_different_subject_alone_blocks_the_body_rule(self):
        self.assertEqual(self.pair(mail(subject="Job one", date=T1), mail(subject="Job two", date=T2)), {})

    def test_the_subject_alone_never_marks_anything(self):
        self.assertEqual(self.pair(mail(subject="Same subject", date=T1, body=BODY + " first"),
                                   mail(subject="RESEND: Same subject", date=T2, body=BODY + " second")), {})

    def test_a_thin_body_is_never_enough(self):
        self.assertEqual(self.pair(mail(date=T1, body="Thanks!"), mail(date=T2, body="Thanks!")), {})

    def test_the_same_new_text_over_different_quoted_history_is_not_a_duplicate(self):
        one = BODY + "\n\nOn Mon, Nov 2, 2026 at 9:00 AM Sam <s@x.test> wrote:\n> quote number one with its own items"
        two = BODY + "\n\nOn Mon, Nov 2, 2026 at 9:00 AM Sam <s@x.test> wrote:\n> quote number two with other items"
        self.assertEqual(self.pair(mail(date=T1, body=one), mail(date=T2, body=two)), {})

    def test_a_different_pasted_picture_is_not_a_duplicate(self):
        d = self.pair(mail(date=T1, body=BODY, inline=[(png(300, 100, "red"), "p.png")]),
                      mail(date=T2, body=BODY, inline=[(png(300, 100, "blue"), "p.png")]))
        self.assertEqual(d, {})

    def test_different_sender_is_not_a_duplicate(self):
        self.assertEqual(self.pair(mail(date=T1), mail(frm="Other <o@acme.test>", date=T2)), {})


class DateRuleTests(Base):
    def att(self, **kw):
        return mail(attachments=[(SHEET_A, "l.xlsx")], **kw)

    def test_tied_dates_are_not_marked(self):
        self.assertEqual(self.pair(self.att(date=T1), self.att(date=T1)), {})
        self.assertEqual(self.pair(self.att(date="Mon, 02 Nov 2026 09:00:00 -0600"),
                                   self.att(date="Mon, 02 Nov 2026 10:00:00 -0500")), {})     # same instant, different zone

    def test_missing_or_broken_dates_are_not_marked(self):
        self.assertEqual(self.pair(self.att(date=T1), self.att(date=None)), {})
        self.assertEqual(self.pair(self.att(date=T1), self.att(date="sometime last week")), {})
        self.assertEqual(self.pair(self.att(date=None), self.att(date=None)), {})

    def test_the_later_date_is_the_duplicate_whatever_the_ids_say(self):
        d = self.pair(self.att(date=T2), self.att(date=T1), ids=("A1", "Z9"))     # Z9 is the earlier one
        self.assertEqual(d["A1"].original, "Z9")
        self.assertNotIn("Z9", d)

    def test_dates_in_different_time_zones_compare_as_instants(self):
        d = self.pair(self.att(date="Mon, 02 Nov 2026 09:00:00 -0600"), self.att(date="Mon, 02 Nov 2026 15:30:00 +0000"))
        self.assertEqual(list(d), ["A2"])                                         # 15:00 UTC then 15:30 UTC


class ChainAndSafetyTests(Base):
    def att(self, **kw):
        return mail(attachments=[(SHEET_A, "l.xlsx")], **kw)

    def test_a_chain_of_three_points_everything_at_the_earliest_and_the_original_is_not_a_duplicate(self):
        self.put("C3", self.att(date="Wed, 04 Nov 2026 09:00:00 -0600"))
        self.put("A1", self.att(date=T1))
        self.put("B2", self.att(date=T2))
        d = self.find()
        self.assertEqual(sorted(d), ["B2", "C3"])
        self.assertEqual({k: v.original for k, v in d.items()}, {"B2": "A1", "C3": "A1"})
        self.assertNotIn("A1", d)

    def test_a_tie_at_the_earliest_date_marks_nothing_in_that_group(self):
        self.put("A1", self.att(date=T1))
        self.put("A2", self.att(date=T1))
        self.put("A3", self.att(date=T2))
        self.assertEqual(self.find(), {})

    def test_a_third_party_in_between_does_not_break_the_chain(self):
        self.put("A1", self.att(date=T1))
        self.put("B2", mail(frm="Other <o@acme.test>", attachments=[(SHEET_A, "l.xlsx")], date=T2))
        self.put("C3", self.att(date="Wed, 04 Nov 2026 09:00:00 -0600"))
        d = self.find()
        self.assertEqual(list(d), ["C3"])
        self.assertEqual(d["C3"].original, "A1")

    def test_unreadable_files_and_missing_senders_are_left_alone(self):
        self.put("A1", self.att(date=T1))
        (self.emails / "A2.eml").write_bytes(b"\x00\xff not an email")
        nosender = self.att(date=T2)
        del nosender["From"]
        self.put("A3", nosender)
        self.assertEqual(self.find(), {})

    def test_detection_failure_never_stops_a_run(self):
        from unittest import mock
        self.put("A1", mail(date=T1))
        with mock.patch.object(PL, "find_duplicates", side_effect=RuntimeError("bug")):
            res = PL.run_parse(self.emails, FakeClient(lambda p: answer()), cache=C.Cache(self.root / "c"),
                               ledger=L.Ledger(self.root / "l.jsonl"), stats=CallStats())
        self.assertEqual(list(res), ["A1"])


class PipelineIntegrationTests(Base):
    def setUp(self):
        super().setUp()
        self.cache, self.ledger, self.stats = C.Cache(self.root / "cache"), L.Ledger(self.root / "l.jsonl"), CallStats()

    def run_parse(self, client, **kw):
        notes = {}
        res = PL.run_parse(self.emails, client, cache=self.cache, ledger=self.ledger, stats=self.stats, notes=notes, **kw)
        return res, notes

    def test_a_duplicate_gets_no_model_call_and_takes_the_originals_classification(self):
        self.put("E1", mail(subject="Shutdown list", date=T1, attachments=[(SHEET_A, "l.xlsx")]))
        self.put("E2", mail(subject="RESEND: Shutdown list", date=T2, attachments=[(SHEET_A, "l.xlsx")]))
        self.put("E3", mail(frm="Other <o@acme.test>", subject="Different customer", body="Another request entirely, please price two valves.",
                            date=T2))
        def responder(p):
            text = p["user"][0]["text"]
            return answer(cls="purchase_order" if "Shutdown list" in text else "rfq",
                          lines=[line(source="l.xlsx" if "ATTACHMENT: l.xlsx" in text else "body")])
        client = FakeClient(responder)
        res, notes = self.run_parse(client)
        self.assertEqual(len(client.models), 2)                                   # E1 and E3 only
        self.assertEqual(res["E2"], {"classification": "purchase_order", "revises": None, "duplicate_of": "E1",
                                     "needs_review": False, "review_reason": None, "line_items": []})
        self.assertEqual(res["E1"]["classification"], "purchase_order")
        self.assertEqual(len(res["E1"]["line_items"]), 1)                         # the original keeps its lines
        self.assertIsNone(res["E3"]["duplicate_of"])
        self.assertIn("duplicate of E1", notes["E2"][0])
        self.assertIn("no model call", notes["E2"][0])

    def test_the_original_is_processed_even_when_only_the_duplicate_is_asked_for(self):
        self.put("E1", mail(date=T1, attachments=[(SHEET_A, "l.xlsx")]))
        self.put("E2", mail(date=T2, attachments=[(SHEET_A, "l.xlsx")]))
        client = FakeClient(lambda p: answer(cls="rfq"))
        res, _ = self.run_parse(client, only=["E2"])
        self.assertEqual(list(res), ["E2"])
        self.assertEqual((res["E2"]["duplicate_of"], res["E2"]["classification"]), ("E1", "rfq"))
        self.assertEqual(len(client.models), 1)                                   # E1 was needed to know the class

    def test_a_replay_with_no_key_is_identical(self):
        self.put("E1", mail(date=T1, attachments=[(SHEET_A, "l.xlsx")]))
        self.put("E2", mail(date=T2, attachments=[(SHEET_A, "l.xlsx")]))
        first, _ = self.run_parse(FakeClient(lambda p: answer()))
        second, _ = self.run_parse(None)
        self.assertEqual(first, second)

    def test_a_duplicate_of_a_revision_keeps_the_revises_link(self):
        a = mail(subject="Shutdown list", date=T1, attachments=[(SHEET_A, "l.xlsx")])
        del a["Message-ID"]; a["Message-ID"] = "<m1@x.test>"; a["In-Reply-To"] = "<m0@x.test>"
        b = mail(subject="Shutdown list", date=T2, attachments=[(SHEET_A, "l.xlsx")])
        del b["Message-ID"]; b["Message-ID"] = "<m2@x.test>"; b["In-Reply-To"] = "<m0@x.test>"
        root = mail(subject="First", date="Sun, 01 Nov 2026 09:00:00 -0600", body="the very first request, long enough to be a body.")
        del root["Message-ID"]; root["Message-ID"] = "<m0@x.test>"
        self.put("E0", root); self.put("E1", a); self.put("E2", b)
        res, _ = self.run_parse(FakeClient(lambda p: answer(cls="rfq_revision") if "Shutdown" in p["user"][0]["text"] else answer()))
        self.assertEqual((res["E1"]["revises"], res["E2"]["duplicate_of"], res["E2"]["revises"]), ("E0", "E1", "E0"))


class SubjectNormalisationTests(unittest.TestCase):
    def test_prefixes_are_stripped_repeatedly_and_case_insensitively(self):
        self.assertEqual(D.normalize_subject("RE: RESEND - Fwd:  Pricing   List"), "pricing list")
        self.assertEqual(D.normalize_subject("Resend pricing"), "resend pricing")       # no colon: a word, not a tag
        self.assertEqual(D.normalize_subject(None), "")


if __name__ == "__main__":
    unittest.main()
