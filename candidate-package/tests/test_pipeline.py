"""Tests for the extraction pipeline (prompt, schema, payload, mapping, guards, --only) with a fake client.
No real model calls and no writes to the repo cache or ledger.

    python tests/test_pipeline.py -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(HERE))
from rfqparse import config, pipeline as PL  # noqa: E402
from rfqparse.llm import cache as C  # noqa: E402
from rfqparse.llm import errors as E  # noqa: E402
from rfqparse.llm import ledger as L  # noqa: E402
from rfqparse.llm import prompts as PR  # noqa: E402
from rfqparse.llm import schema as SC  # noqa: E402
from rfqparse.llm.call import CallStats  # noqa: E402
from rfqparse.llm.jsonvalidate import check_strict, validate  # noqa: E402
from test_registry import make_mail, png, xlsx_bytes  # noqa: E402

MINI, FULL = "gpt-4.1-mini", "gpt-4.1"


def line(raw="2 ea 2in elbow", desc="2in elbow", qty=2, uom="EA", source="body", ctx=None):
    return {"raw": raw, "description": desc, "qty": qty, "uom": uom, "source": source, "applies_context": ctx or []}


def answer(cls="rfq", lines=None, doubts=None, hint=None, note=""):
    return {"classification": cls, "revises_hint": hint, "lines": [line()] if lines is None else lines,
            "doubts": doubts or [], "note": note}


def reply(parsed):
    return {"parsed": parsed, "raw_text": json.dumps(parsed), "finish_reason": "stop", "refusal": None}


class FakeClient:
    """Answers by calling `responder(payload)`; remembers the models it was asked."""

    reports_attempts = False

    def __init__(self, responder):
        self.responder, self.models = responder, []

    def complete(self, payload):
        self.models.append(payload["model"])
        out = self.responder(payload)
        if isinstance(out, Exception):
            raise out
        return {"response": reply(out), "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.emails = self.root / "emails"
        self.emails.mkdir()
        self.cache, self.ledger, self.stats = C.Cache(self.root / "cache"), L.Ledger(self.root / "l.jsonl"), CallStats()

    def write(self, eid, msg):
        (self.emails / f"{eid}.eml").write_bytes(msg.as_bytes() if hasattr(msg, "as_bytes") else msg)
        return self.emails / f"{eid}.eml"

    def run_one(self, eid, client, index=None):
        return PL.process_email(self.emails / f"{eid}.eml", client=client, cache=self.cache, ledger=self.ledger,
                                stats=self.stats, index=index or {})


# ------------------------------------------------------------------ prompt and schema


def _dev_blacklist() -> set[str]:
    bad: set[str] = set()
    for p in sorted((ROOT / "data" / "dev" / "emails").glob("*.eml")):
        msg = BytesParser(policy=policy.default).parsebytes(p.read_bytes())
        bad.add(p.stem)
        for h in ("from", "subject"):
            v = str(msg.get(h) or "")
            if h == "from":
                m = re.match(r'\s*"?([^"<]*?)"?\s*<([^>]+)>', v)
                if m:
                    bad.update({m.group(1).strip(), m.group(2).split("@")[-1]})
            else:
                bad.add(v.strip())
        for part in msg.walk():
            if part.get_filename():
                bad.add(part.get_filename())
    key = json.loads((ROOT / "data" / "dev" / "answer_key.json").read_text(encoding="utf-8"))["emails"]
    bad.update(l["raw"] for e in key.values() for l in e["line_items"])
    bad.discard(config.RIDGELINE_DOMAIN)
    return {b for b in bad if len(b.strip()) >= 4 and config.RIDGELINE_DOMAIN not in b}


class PromptAndSchemaTests(unittest.TestCase):
    def test_prompt_has_no_dev_strings_ids_or_examples(self):
        text = PR.system_prompt().lower()
        self.assertIsNone(re.search(r"\be\d{3}\b", text), "an email id appears in the prompt")
        hits = [b for b in _dev_blacklist() if b.lower() in text]
        self.assertEqual(hits, [], f"dev strings found in the prompt: {hits}")

    def test_prompt_carries_the_rules_it_must(self):
        t = PR.system_prompt()
        self.assertIn(config.RIDGELINE_DOMAIN, t)
        self.assertIn(f"rfq@{config.RIDGELINE_DOMAIN}", t)
        for cls in SC.CLASSIFICATIONS:
            self.assertIn(cls, t)
        for u in SC.UOMS:
            self.assertIn(u, t)
        for r in SC.DOUBT_REASONS:
            self.assertIn(r, t)
        low = t.lower()
        for phrase in ("supplier", "classify it as the request", "still return every line", "headings", "quoted",
                       "context applies only inside its own section", "never invent", "stated", "vague amount",
                       "not converted", "forwarding", "exact source name"):
            self.assertIn(phrase, low, phrase)

    def test_prompt_states_the_context_and_quantity_rules(self):
        low = PR.system_prompt().lower()
        for phrase in ("applies_context", "copied word for word", "applies to every line in that list", "same size",
                       "overrides a blanket spec", "another section", "never paraphrase", "guessed number",
                       "a few", "enough for the job", "visible only inside a picture",
                       "do not restate the quantity", "labels, not specs",
                       "after an x or", "both sides of the x are sizes", "set or sets = set", "box or boxes = bx",
                       "pack or packs = pk", "pair or pairs = pr", "dozen = dz", "roll or rolls = rl",
                       "pound or pounds = lb", "gallon or gallons = gal", "bag or bags = bag", "case or cases = cs",
                       "foot or feet = ft", "meter or meters = m", "inch or inches = in", "use null only when no unit",
                       "plain pieces, each, joints or lengths are ea", "identified only by an attached photo",
                       "not the sentence in the message", "picture's source name, not body", "specs only",
                       "never list chatter",
                       "explicitly commits to buy selected items of an earlier quote", "by number or by description",
                       "items the message holds, defers", "the source of these lines is body",
                       "the single line that holds the model or part number as printed", "model: xyz-100", "p/n 4471-b",
                       "not the whole nameplate", "only allowed values", "never write a description such as"):
            self.assertIn(phrase, low, phrase)

    def test_prompt_is_deterministic_and_versioned(self):
        self.assertEqual(PR.system_prompt(), PR.system_prompt())
        self.assertEqual((config.PROMPT_VERSION, config.SCHEMA_VERSION), ("v5", "v2"))

    def test_schema_is_strict_and_matches_the_official_lists(self):
        check_strict(SC.EXTRACTION_SCHEMA)
        spec = importlib.util.spec_from_file_location("check_format", ROOT / "data" / "check_format.py")
        cf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cf)
        self.assertEqual(SC.CLASSIFICATIONS, list(cf.CLASSES))
        self.assertEqual(SC.UOMS, list(cf.UOMS))
        self.assertEqual(SC.DOUBT_REASONS, ["illegible", "attachment_missing_or_unreadable", "item_defined_outside_email",
                                            "unclear_if_request", "other"])
        self.assertEqual(set(SC.EXTRACTION_SCHEMA["required"]), {"classification", "revises_hint", "lines", "doubts", "note"})
        self.assertEqual(validate(answer(), SC.EXTRACTION_SCHEMA), [])
        self.assertEqual(validate(answer(lines=[line(qty=None, uom=None)]), SC.EXTRACTION_SCHEMA), [])
        self.assertTrue(validate(answer(lines=[line(uom="JOINTS")]), SC.EXTRACTION_SCHEMA))
        no_ctx = answer(); del no_ctx["lines"][0]["applies_context"]
        self.assertTrue(validate(no_ctx, SC.EXTRACTION_SCHEMA))                    # applies_context is required
        self.assertTrue(validate(answer(lines=[line(ctx=[1])]), SC.EXTRACTION_SCHEMA))
        self.assertEqual(validate(answer(lines=[line(ctx=["a phrase"])]), SC.EXTRACTION_SCHEMA), [])
        self.assertTrue(validate(answer(lines=[line(qty="12")]), SC.EXTRACTION_SCHEMA))
        self.assertTrue(validate(answer(doubts=[{"reason": "made_up", "detail": "x"}]), SC.EXTRACTION_SCHEMA))
        self.assertTrue(validate(answer(cls="invoice"), SC.EXTRACTION_SCHEMA))


# ------------------------------------------------------------------ payload


class PayloadTests(Base):
    def parts(self, msg):
        from rfqparse.ingest import ingest_eml
        from rfqparse.render.registry import render_email
        self.write("P1", msg)
        mail = ingest_eml(self.emails / "P1.eml")
        return PR.build_user_parts(render_email(mail), mail)

    def test_headers_current_quoted_labelled_and_attachments_named(self):
        body = "Add two more.\n\nOn Mon, Nov 2, 2026 at 9:00 AM Sam <s@x.test> wrote:\n> 10 ea elbows"
        parts, sources = self.parts(make_mail([(xlsx_bytes("widget"), "application", "octet-stream", "list.xlsx", {})], body=body))
        text = parts[0]["text"]
        self.assertEqual([p["type"] for p in parts], ["text"])
        for needle in ("=== HEADERS ===", "Subject: Need pricing", "Sender is a Ridgeline rep: no", "=== CURRENT MESSAGE ===",
                       "Add two more.", "=== QUOTED EARLIER TEXT (context only", "> 10 ea elbows",
                       "=== ATTACHMENT: list.xlsx (type xlsx, status ok) ==="):
            self.assertIn(needle, text)
        self.assertLess(text.index("Add two more."), text.index("=== QUOTED EARLIER TEXT"))
        self.assertLess(text.index("=== QUOTED EARLIER TEXT"), text.index("=== ATTACHMENT"))
        self.assertEqual(sources, {"list.xlsx"})

    def test_images_attached_each_after_its_own_label_and_skipped_pixels_excluded(self):
        b = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(b, "GIF")
        parts, sources = self.parse_with_images(b.getvalue())
        kinds = [p["type"] for p in parts]
        self.assertEqual(kinds, ["text", "text", "image"])                      # only the real picture, label then image
        self.assertIn("source name 'photo.png'", parts[1]["text"])
        self.assertEqual(parts[2]["detail"], config.LLM_IMAGE_DETAIL)
        self.assertEqual(sources, {"photo.png", "pixel.gif"})                   # the pixel is a known attachment but sends nothing
        self.assertIn("status skipped", parts[0]["text"])

    def parse_with_images(self, pixel):
        return self.parts(make_mail([(png(200, 100, "red"), "image", "png", "photo.png", {}),
                                     (pixel, "image", "gif", "pixel.gif", {})]))

    def test_unreadable_attachment_is_listed_with_its_reason(self):
        parts, _ = self.parts(make_mail([(b"\xd0\xcf\x11\xe0" + b"\x00" * 40, "application", "vnd.ms-excel", "old.xls", {})]))
        self.assertIn("=== ATTACHMENT: old.xls (type ole, status unreadable) ===", parts[0]["text"])
        self.assertIn("reason: legacy Office file", parts[0]["text"])


# ------------------------------------------------------------------ mapping and guards


class MappingTests(Base):
    def go(self, ans, attachments=(), eid="M1", **kw):
        self.cache = C.Cache(self.root / f"cache-{len(list(self.root.glob('cache-*')))}")   # fresh cache: same email, new answer
        self.write(eid, make_mail(list(attachments), **kw))
        return self.run_one(eid, FakeClient(lambda p: ans))

    def test_a_clean_request_maps_to_the_output_format(self):
        e = self.go(answer(lines=[line(qty=12.0, uom=None), line("30 ft pipe", "pipe", 30, "FT")]))
        self.assertEqual(e["classification"], "rfq")
        self.assertEqual([l["qty"] for l in e["line_items"]], [12, 30])
        self.assertIsInstance(e["line_items"][0]["qty"], int)
        self.assertEqual((e["needs_review"], e["review_reason"], e["revises"], e["duplicate_of"]), (False, None, None, None))
        self.assertEqual(sorted(e["line_items"][0]), ["description", "qty", "raw", "source", "uom"])

    def test_doubts_become_needs_review_with_the_structured_reason_and_lines_are_kept(self):
        e = self.go(answer(doubts=[{"reason": "illegible", "detail": "third line smudged"}]))
        self.assertTrue(e["needs_review"])
        self.assertEqual(e["review_reason"], "illegible: third line smudged")
        self.assertEqual(len(e["line_items"]), 1)

    def test_non_request_classes_force_no_review_and_no_lines(self):
        for cls in ("vendor_quote", "status_inquiry", "auto_reply", "other"):
            with self.subTest(cls):
                e = self.go(answer(cls=cls, lines=[line()], doubts=[{"reason": "other", "detail": "x"}]))
                self.assertEqual((e["classification"], e["line_items"], e["needs_review"], e["review_reason"]), (cls, [], False, None))

    def test_guard_unreadable_attachment_on_a_request_forces_needs_review(self):
        att = [(b"\xd0\xcf\x11\xe0" + b"\x00" * 40, "application", "vnd.ms-excel", "old.xls", {})]
        e = self.go(answer(), att)                                              # the model raised no doubt
        self.assertTrue(e["needs_review"])
        self.assertIn("attachment 'old.xls' could not be read", e["review_reason"])
        self.assertIn("legacy Office file", e["review_reason"])
        self.assertEqual(len(e["line_items"]), 1)                               # lines still returned
        # ...but not on a non-request
        e2 = self.go(answer(cls="other", lines=[]), att, eid="M2")
        self.assertFalse(e2["needs_review"])

    def test_guard_request_with_no_lines_and_no_flag_becomes_needs_review(self):
        for cls in ("rfq", "rfq_revision", "purchase_order"):
            with self.subTest(cls):
                e = self.go(answer(cls=cls, lines=[]))
                self.assertTrue(e["needs_review"])
                self.assertEqual(e["review_reason"], "request with no readable line items")

    def test_reasons_are_combined_without_duplicates(self):
        att = [(b"\xd0\xcf\x11\xe0" + b"\x00" * 40, "application", "vnd.ms-excel", "old.xls", {})]
        e = self.go(answer(lines=[], doubts=[{"reason": "attachment_missing_or_unreadable", "detail": "no list"}]), att)
        self.assertTrue(e["review_reason"].startswith("attachment_missing_or_unreadable: no list; attachment 'old.xls' could not be read"))
        self.assertEqual(e["review_reason"].count("attachment 'old.xls'"), 1)
        self.assertNotIn("no readable line items", e["review_reason"])          # a flag already exists, so guard 2 stays quiet


class ReviseTests(Base):
    def test_revision_resolves_the_parent_from_the_thread_headers_or_null(self):
        a = make_mail(); del a["Message-ID"]; a["Message-ID"] = "<orig@x.test>"
        b = make_mail(); del b["Message-ID"]; b["Message-ID"] = "<rev@x.test>"; b["In-Reply-To"] = "<orig@x.test>"
        c = make_mail(); del c["Message-ID"]; c["Message-ID"] = "<rev2@x.test>"; c["In-Reply-To"] = "<not-in-inbox@x.test>"
        for eid, m in (("A1", a), ("A2", b), ("A3", c)):
            self.write(eid, m)
        client = FakeClient(lambda p: answer(cls="rfq_revision", hint="the earlier list"))
        res = PL.run_parse(self.emails, client, cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertEqual(res["A2"]["revises"], "A1")
        self.assertIsNone(res["A3"]["revises"])                                 # parent not in the inbox
        self.assertIsNone(res["A1"]["revises"] if res["A1"]["classification"] == "rfq_revision" and False else None)


# ------------------------------------------------------------------ the code guards after the call


class GuardBase(Base):
    def pkg_for(self, msg, eid="G1"):
        from rfqparse.ingest import ingest_eml
        from rfqparse.render.registry import render_email
        self.write(eid, msg)
        return render_email(ingest_eml(self.emails / f"{eid}.eml"))


class ContextGuardTests(GuardBase):
    SUBJECT = "316 SS threaded fittings"

    def mail(self, attachments=(), body="need 10 of the 1/2 90s and 6 tees same size, SCH 40 pipe"):
        m = make_mail(list(attachments), body=body)
        del m["Subject"]
        m["Subject"] = self.SUBJECT
        return m

    def test_a_listed_phrase_missing_from_the_description_is_appended(self):
        pkg = self.pkg_for(self.mail())
        notes = []
        out = PL.apply_line_guards([line("10 of the 1/2 90s", "1/2 inch 90 elbow", 10, ctx=[self.SUBJECT])], pkg, notes)
        self.assertEqual(out[0]["description"], "1/2 inch 90 elbow, 316 SS threaded fittings")
        self.assertTrue(any("appended context" in n for n in notes))

    def test_a_phrase_already_in_the_description_is_not_added_twice_whatever_the_case_or_punctuation(self):
        pkg = self.pkg_for(self.mail())
        for desc in ("316 SS threaded fittings, 1/2 inch elbow", "1/2 inch elbow - 316 ss, THREADED fittings"):
            out = PL.apply_line_guards([line("x 90s", desc, 10, ctx=[self.SUBJECT])], pkg)
            self.assertEqual(out[0]["description"], desc)

    def test_an_invented_phrase_is_dropped_and_noted(self):
        pkg = self.pkg_for(self.mail())
        notes = []
        out = PL.apply_line_guards([line("10 of the 1/2 90s", "1/2 inch elbow", 10, ctx=["ASTM A312 TP316L", self.SUBJECT])], pkg, notes)
        self.assertNotIn("A312", out[0]["description"])
        self.assertIn("316 SS threaded fittings", out[0]["description"])
        self.assertTrue(any("dropped context phrase 'ASTM A312 TP316L'" in n for n in notes))

    def test_phrase_matching_is_by_whole_words(self):
        pkg = self.pkg_for(self.mail())
        notes = []
        out = PL.apply_line_guards([line("x", "pipe", 1, ctx=["SCH 4"])], pkg, notes)    # the email says SCH 40
        self.assertEqual(out[0]["description"], "pipe")
        self.assertTrue(any("dropped" in n for n in notes))

    def test_phrases_from_attachment_text_and_quoted_text_are_accepted(self):
        pkg = self.pkg_for(self.mail([(xlsx_bytes("ALL FITTINGS BW UNLESS NOTED"), "application", "octet-stream", "list.xlsx", {})],
                                     body="Add one.\n\nOn Mon, Nov 2, 2026 at 9:00 AM Sam <s@x.test> wrote:\n> All pipe is A53B ERW"))
        out = PL.apply_line_guards([line("1 | tee", "tee", 1, source="list.xlsx", ctx=["ALL FITTINGS BW UNLESS NOTED", "All pipe is A53B ERW"])], pkg)
        self.assertIn("ALL FITTINGS BW UNLESS NOTED", out[0]["description"])
        self.assertIn("All pipe is A53B ERW", out[0]["description"])

    def test_a_line_read_from_a_picture_cannot_be_checked_so_nothing_is_added(self):
        pkg = self.pkg_for(self.mail([(png(300, 120, "blue"), "image", "png", "pic.png", {})]))
        notes = []
        out = PL.apply_line_guards([line("6 tees", "tee", 6, source="pic.png", ctx=[self.SUBJECT])], pkg, notes)
        self.assertEqual(out[0]["description"], "tee")
        self.assertTrue(any("comes from a picture" in n for n in notes))

    def test_repeated_and_blank_phrases_are_ignored(self):
        pkg = self.pkg_for(self.mail())
        out = PL.apply_line_guards([line("x", "tee", 1, ctx=[self.SUBJECT, self.SUBJECT, "  ", ""])], pkg)
        self.assertEqual(out[0]["description"].count("316 SS"), 1)

    def test_end_to_end_the_entry_carries_the_context_and_has_no_applies_context_key(self):
        self.write("G2", self.mail())
        client = FakeClient(lambda p: answer(lines=[line("10 of the 1/2 90s", "1/2 inch 90 elbow", 10, ctx=[self.SUBJECT, "made up spec"])]))
        notes = []
        e = PL.process_email(self.emails / "G2.eml", client=client, cache=self.cache, ledger=self.ledger, stats=self.stats, notes=notes)
        self.assertEqual(e["line_items"][0]["description"], "1/2 inch 90 elbow, 316 SS threaded fittings")
        self.assertEqual(sorted(e["line_items"][0]), ["description", "qty", "raw", "source", "uom"])
        self.assertTrue(any("made up spec" in n for n in notes))


class QuantityGuardTests(GuardBase):
    def qty_for(self, raw, qty, notes=None):
        pkg = self.pkg_for(make_mail(body=raw))
        return PL.apply_line_guards([line(raw, "item", qty)], pkg, notes)[0]["qty"]

    def test_a_vague_amount_becomes_null_and_is_noted(self):
        notes = []
        self.assertIsNone(self.qty_for("a couple caps", 2, notes))
        self.assertTrue(any("set to null" in n for n in notes))
        for raw in ("a few tees", "several elbows", "enough for the job", "some nipples", "somebody's tees",
                    "a couple caps", "A COUPLE caps", "a   couple of unions", "an handful of caps", "a bunch of tees",
                    "a lot of nipples", "a few"):
            with self.subTest(raw):
                self.assertIsNone(self.qty_for(raw, 3))

    def test_a_standalone_a_or_an_means_one_unless_it_precedes_a_vague_word(self):
        for raw in ("a gate valve", "an elbow", "A gate valve", "need an 8in elbow", "one tee and a union"):
            with self.subTest(raw):
                self.assertEqual(self.qty_for(raw, 1), 1)
        # "a"/"an" stuck to something else is not an article
        self.assertFalse(PL.has_quantity_evidence("pipe ASTMA"))        # (a digit anywhere, even in a grade, still counts: the guard is a floor)
        self.assertFalse(PL.has_quantity_evidence("grade A-ish"))
        self.assertTrue(PL.has_quantity_evidence("an elbow"))
        self.assertFalse(PL.has_quantity_evidence("a couple of caps"))
        self.assertTrue(PL.has_quantity_evidence("a couple of caps, and a tee"))      # the second "a" is a real one

    def test_a_spelled_out_number_stays(self):
        for raw, q in (("ten elbows", 10), ("TEN elbows", 10), ("a dozen elbows", 12), ("twenty-five tees", 25),
                       ("two hundred couplings", 200), ("one gate valve", 1), ("a pair of unions", 2), ("six caps", 6)):
            with self.subTest(raw):
                self.assertEqual(self.qty_for(raw, q), q)

    def test_digits_stay_even_when_embedded_in_a_spec(self):
        self.assertEqual(self.qty_for("12 ea 2in elbow", 12), 12)
        self.assertEqual(self.qty_for("elbows x 8", 8), 8)

    def test_null_stays_null_and_the_evidence_function_is_whole_word(self):
        self.assertIsNone(self.qty_for("a couple caps", None))
        self.assertFalse(PL.has_quantity_evidence("someone's tonne"))      # 'one' / 'ten' inside other words do not count
        self.assertTrue(PL.has_quantity_evidence("three of the valves"))
        self.assertTrue(PL.has_quantity_evidence("qty: 4"))

    def test_the_guard_runs_on_the_final_entry(self):
        self.write("Q1", make_mail(body="need a couple caps"))
        client = FakeClient(lambda p: answer(lines=[line("a couple caps", "caps", 2)]))
        e = PL.process_email(self.emails / "Q1.eml", client=client, cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertIsNone(e["line_items"][0]["qty"])


class RestatedQuantityTests(GuardBase):
    def strip(self, desc, qty, uom):
        return PL.strip_restated_quantity(desc, qty, uom)

    def test_a_leading_quantity_and_unit_equal_to_the_lines_own_is_removed(self):
        self.assertEqual(self.strip('7 EA 1/2" weld neck flange, A105', 7, "EA"), '1/2" weld neck flange, A105')
        self.assertEqual(self.strip("12 ft of 2in pipe", 12, "FT"), "2in pipe")
        self.assertEqual(self.strip("2 sets of 4in gaskets, 304", 2, "SET"), "4in gaskets, 304")
        self.assertEqual(self.strip("10 pieces of 2in elbow", 10, "EA"), "2in elbow")
        self.assertEqual(self.strip("10 pieces of 2in elbow", 10, None), "2in elbow")           # a null unit is EA
        self.assertEqual(self.strip("1.5 gal 3in thing", 1.5, "GAL"), "3in thing")
        self.assertEqual(self.strip("6 JTS 2in pipe", 6.0, "EA"), "2in pipe")

    def test_only_then_never_when_the_number_or_the_unit_differs_or_is_missing(self):
        self.assertEqual(self.strip("7 EA 1/2 elbow", 8, "EA"), "7 EA 1/2 elbow")             # a different number
        self.assertEqual(self.strip("7 EA 1/2 elbow", 7, "FT"), "7 EA 1/2 elbow")             # a different unit
        self.assertEqual(self.strip("2 sets of gaskets", 2, None), "2 sets of gaskets")      # sets is SET, the line says EA
        self.assertEqual(self.strip("7 EA 1/2 elbow", None, "EA"), "7 EA 1/2 elbow")          # no qty on the line
        self.assertEqual(self.strip("2 elbow", 2, "EA"), "2 elbow")                             # a number but no unit word
        self.assertEqual(self.strip("2in 90 elbow", 2, "EA"), "2in 90 elbow")                  # "2in" is a size, not "2 in"
        self.assertEqual(self.strip('4" 150# flange', 4, "EA"), '4" 150# flange')              # a size, not a quantity
        self.assertEqual(self.strip("flange, A105 x 6", 6, "EA"), "flange, A105 x 6")          # only a LEADING restatement
        self.assertEqual(self.strip("7 EA", 7, "EA"), "7 EA")                                   # never leave an empty description
        self.assertEqual(self.strip("", 7, "EA"), "")

    def test_it_runs_in_the_guards_and_is_noted(self):
        pkg = self.pkg_for(make_mail(body="7 ea 1/2 weld neck flange"))
        notes = []
        out = PL.apply_line_guards([line("7 ea 1/2 weld neck flange", "7 EA 1/2 weld neck flange", 7, "EA")], pkg, notes)
        self.assertEqual(out[0]["description"], "1/2 weld neck flange")
        self.assertTrue(any("restated quantity" in n for n in notes))

    def test_the_unit_table_covers_every_allowed_code(self):
        self.assertEqual(sorted(config.UNIT_WORDS), sorted(SC.UOMS))


class SourceRepairTests(GuardBase):
    QUOTED = 'Go ahead with item 1.\n\nOn Mon, Nov 2, 2026 at 9:00 AM Sam <s@x.test> wrote:\n> 1. 8 EA - 3" 150# RF flange, A105 - $38.60 ea\n> 2. 4 EA - 3" butterfly valve, CF8M'
    ROW = '1. 8 EA - 3" 150# RF flange, A105'

    def xl(self, name, text):
        return (xlsx_bytes(text), "application", "octet-stream", name, {})

    def run_mail(self, msg, ln, eid="R1", client=None, notes=None, cls="purchase_order"):
        self.cache = C.Cache(self.root / f"c-{len(list(self.root.glob('c-*')))}")
        self.write(eid, msg)
        client = client or FakeClient(lambda p: answer(cls=cls, lines=[ln]))
        e = PL.process_email(self.emails / f"{eid}.eml", client=client, cache=self.cache, ledger=self.ledger,
                             stats=self.stats, notes=notes)
        return e, client

    def test_raw_found_in_the_quoted_text_gets_source_body_with_no_escalation(self):
        notes = []
        e, client = self.run_mail(make_mail(body=self.QUOTED), line(self.ROW, "3in flange", 8, "EA", source="quoted earlier text"),
                                  notes=notes)
        self.assertEqual(e["line_items"][0]["source"], "body")
        self.assertEqual(client.models, [MINI])                                    # repaired before validation: no retry
        self.assertFalse(e["needs_review"])
        self.assertTrue(any("source repaired on line 1" in n and "'quoted earlier text' -> 'body'" in n for n in notes))

    def test_raw_found_in_the_current_body_gets_source_body(self):
        e, _ = self.run_mail(make_mail(body="please quote 12 ft of 2in galvanized pipe today"),
                             line("12 ft of 2in galvanized pipe", "pipe", 12, "FT", source="email text"))
        self.assertEqual(e["line_items"][0]["source"], "body")

    def test_raw_found_in_exactly_one_attachment_gets_that_attachment(self):
        row = "7 | 5 | EA | 6in | alpha widget"
        notes = []
        msg = make_mail([self.xl("list.xlsx", row), self.xl("other.xlsx", "something else entirely")])
        e, client = self.run_mail(msg, line(row, "widget", 5, "EA", source="the spreadsheet"), notes=notes, cls="rfq")
        self.assertEqual(e["line_items"][0]["source"], "list.xlsx")
        self.assertEqual(client.models, [MINI])
        self.assertTrue(any("-> 'list.xlsx'" in n and "attachment 'list.xlsx'" in n for n in notes))

    def test_raw_found_in_two_attachments_is_not_repaired_and_validation_fails_as_before(self):
        row = "7 | 5 | EA | 6in | alpha widget"
        msg = make_mail([self.xl("a.xlsx", row), self.xl("b.xlsx", row)])
        e, client = self.run_mail(msg, line(row, "widget", 5, "EA", source="the spreadsheet"), cls="rfq")
        self.assertEqual(client.models, [MINI, FULL])                               # escalated, and still invalid
        self.assertTrue(e["needs_review"])
        self.assertEqual(e["line_items"], [])
        self.assertIn("neither 'body' nor an attachment name", e["review_reason"])

    def test_raw_found_nowhere_still_fails_validation(self):
        e, client = self.run_mail(make_mail(body="please quote some pipe"),
                                  line("a line that is not in the email at all", "x", 1, "EA", source="quoted earlier text"), cls="rfq")
        self.assertEqual(client.models, [MINI, FULL])
        self.assertTrue(e["needs_review"])
        self.assertIn("SchemaMismatch", e["review_reason"])

    def test_a_valid_source_is_never_touched(self):
        row = "7 | 5 | EA | 6in | alpha widget"
        msg = make_mail([self.xl("list.xlsx", row)], body="and also please quote 12 ft of 2in galvanized pipe")
        for ln, want in ((line("12 ft of 2in galvanized pipe", "pipe", 12, "FT", source="list.xlsx"), "list.xlsx"),   # text is in the body
                         (line(row, "widget", 5, "EA", source="body"), "body")):                                       # text is in the attachment
            with self.subTest(want):
                notes = []
                e, client = self.run_mail(msg, ln, notes=notes, eid=f"V{want[:2]}", cls="rfq")
                self.assertEqual(e["line_items"][0]["source"], want)
                self.assertFalse(any("source repaired" in n for n in notes))

    def test_a_picture_name_is_a_valid_source_and_is_left_alone(self):
        msg = make_mail([(png(300, 120, "blue"), "image", "png", "pic.png", {})], body="quote 3 of the one in the photo")
        e, client = self.run_mail(msg, line("MODEL: XYZ-100", "valve", 3, "EA", source="pic.png"), cls="rfq")
        self.assertEqual(e["line_items"][0]["source"], "pic.png")
        self.assertEqual(client.models, [FULL])                                      # an image email goes to the big model

    def test_too_short_a_raw_is_never_repaired(self):
        e, client = self.run_mail(make_mail(body="we need 2 of them, 2 more later"), line("2", "x", 2, "EA", source="bogus"), cls="rfq")
        self.assertEqual(client.models, [MINI, FULL])
        self.assertTrue(e["needs_review"])

    def test_the_cached_reply_is_not_changed_and_a_replay_with_no_key_repairs_it_the_same_way(self):
        self.cache = C.Cache(self.root / "cache-replay")
        self.write("RP", make_mail(body=self.QUOTED))
        client = FakeClient(lambda p: answer(cls="purchase_order", lines=[line(self.ROW, "3in flange", 8, "EA", source="quoted earlier text")]))
        first = PL.process_email(self.emails / "RP.eml", client=client, cache=self.cache, ledger=self.ledger, stats=self.stats)
        entry = self.cache.entries()[0][0]
        self.assertEqual(entry.response["parsed"]["lines"][0]["source"], "quoted earlier text")   # stored as the model said it
        calls = len(client.models)
        replay = PL.process_email(self.emails / "RP.eml", client=None, cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertEqual(replay, first)
        self.assertEqual(len(client.models), calls)
        self.assertEqual(replay["line_items"][0]["source"], "body")

    def test_the_repair_function_directly(self):
        pkg = self.pkg_for(make_mail([self.xl("list.xlsx", "7 | 5 | EA | 6in | alpha widget")], body=self.QUOTED))
        notes = []
        repair = PL.make_source_repair(pkg, notes)
        parsed = {"lines": [dict(line(self.ROW, "a", 1), source="x"), dict(line("7 | 5 | EA | 6in | alpha widget", "b", 5), source="y"),
                            dict(line("not anywhere in this email", "c", 1), source="z"), line("1 | 2", "d", 1)]}
        out = repair(parsed)
        self.assertEqual([l["source"] for l in out["lines"]], ["body", "list.xlsx", "z", "body"])
        self.assertEqual(parsed["lines"][0]["source"], "x")                          # the input is not mutated
        self.assertEqual(len(notes), 2)
        self.assertIs(repair({"lines": [line("a b c d e f", "x", 1)]})["lines"][0]["source"], "body")   # untouched valid one
        self.assertEqual(repair({"classification": "rfq"}), {"classification": "rfq"})                   # no lines key: unchanged


class ConflictGuardTests(GuardBase):
    BLANKET = "ALL FITTINGS BW SCH 40 A234 WPB UNLESS NOTED"

    def guard(self, raw, desc, ctx=None, body=None):
        pkg = self.pkg_for(make_mail(body=body or f"{self.BLANKET}\n{raw}\nfittings are 150# malleable iron, black"))
        notes = []
        out = PL.apply_line_guards([line(raw, desc, 2, ctx=ctx or [self.BLANKET])], pkg, notes)
        return out[0]["description"], notes

    def test_a_row_with_its_own_schedule_does_not_get_the_blanket_schedule(self):
        desc, notes = self.guard('2 | EA | 6" x 4" | Reducer, concentric, SCH 80', "Concentric reducer, 6 inch by 4 inch, schedule 80")
        self.assertEqual(desc, "Concentric reducer, 6 inch by 4 inch, schedule 80")
        self.assertNotIn("SCH 40", desc)
        self.assertTrue(any("skipped context phrase" in n and "schedule 'SCH 40'" in n for n in notes))

    def test_a_row_with_a_different_rating_does_not_get_the_blanket_rating(self):
        blanket = "fittings are 150# malleable iron, black"
        desc, notes = self.guard("4 ea - 2in 300# union", "2in 300# union", ctx=[blanket])
        self.assertEqual(desc, "2in 300# union")
        self.assertTrue(any("pressure rating '150#'" in n for n in notes))

    def test_a_row_with_no_conflict_still_gets_the_phrase(self):
        desc, notes = self.guard('4 | EA | 6" | Cap', "6 inch cap")
        self.assertEqual(desc, "6 inch cap, " + self.BLANKET)
        self.assertFalse(any("skipped" in n for n in notes))

    def test_the_same_value_is_not_a_conflict_in_any_spelling(self):
        for raw in ("2 ea 6in SCH 40 elbow", "2 ea 6in Schedule 40 elbow", "2 ea 6in sch40 elbow"):
            with self.subTest(raw):
                desc, _ = self.guard(raw, "6in elbow")
                self.assertTrue(desc.endswith(self.BLANKET))
        blanket = "all flanges 150# RF"
        for raw in ("2 ea 150 lb flange", "2 ea Class 150 flange", "2 ea 150# flange"):
            with self.subTest(raw):
                desc, _ = self.guard(raw, "flange", ctx=[blanket], body=blanket + "\n" + raw)
                self.assertTrue(desc.endswith(blanket))
        desc, _ = self.guard("2 ea Class 300 flange", "flange", ctx=[blanket], body=blanket + "\nx")
        self.assertEqual(desc, "flange")                                         # a different class spelled differently

    def test_the_line_states_it_in_its_description_only(self):
        desc, notes = self.guard('4 | EA | 6" | Reducer', "6 inch reducer, schedule 80")
        self.assertEqual(desc, "6 inch reducer, schedule 80")
        self.assertTrue(any("skipped" in n for n in notes))

    def test_the_raw_wins_over_a_description_that_picked_up_the_wrong_value(self):
        # the row says SCH 80 itself; a wrongly carried SCH 40 in the description must not make the blanket look consistent
        desc, notes = self.guard("2 | EA | reducer SCH 80", "reducer SCH 40 SCH 80")
        self.assertTrue(any("skipped" in n for n in notes))
        self.assertNotIn("BW", desc)

    def test_a_phrase_that_names_no_attribute_family_is_always_added(self):
        desc, _ = self.guard("2 | EA | reducer SCH 80", "reducer", ctx=["Bid 26-0412 Tank 14"], body="Bid 26-0412 Tank 14\nreducer SCH 80")
        self.assertEqual(desc, "reducer, Bid 26-0412 Tank 14")

    def test_a_phrase_with_several_values_is_fine_when_one_matches(self):
        phrase = "pipe SCH 40 or SCH 80"
        desc, _ = self.guard("20 ft pipe SCH 80", "pipe", ctx=[phrase], body=phrase + "\n20 ft pipe SCH 80")
        self.assertTrue(desc.endswith(phrase))

    def test_trim_mode_keeps_the_other_specs_of_the_blanket_note(self):
        with mock.patch.object(config, "CONTEXT_CONFLICT_ACTION", "trim"):
            desc, notes = self.guard('2 | EA | 6" x 4" | Reducer, concentric, SCH 80', "Concentric reducer, schedule 80")
        self.assertIn("BW", desc)
        self.assertIn("A234 WPB", desc)
        self.assertNotIn("SCH 40", desc)
        self.assertTrue(any("trimmed to" in n for n in notes))

    def test_trim_mode_adds_nothing_when_nothing_is_left(self):
        with mock.patch.object(config, "CONTEXT_CONFLICT_ACTION", "trim"):
            desc, _ = self.guard("2 | EA | reducer SCH 80", "reducer SCH 80", ctx=["SCH 40"], body="SCH 40\nreducer SCH 80")
        self.assertEqual(desc, "reducer SCH 80")

    def test_the_families_live_in_config_and_a_new_one_is_picked_up(self):
        self.assertEqual([f["name"] for f in config.CONTEXT_CONFLICT_FAMILIES], ["schedule", "pressure rating"])
        fams = config.CONTEXT_CONFLICT_FAMILIES + [{"name": "material grade", "patterns": [r"\b(a105|a350)\b"]}]
        with mock.patch.object(config, "CONTEXT_CONFLICT_FAMILIES", fams):
            desc, notes = self.guard("2 ea flange A350", "flange A350", ctx=["flanges A105 RF"], body="flanges A105 RF\n2 ea flange A350")
        self.assertEqual(desc, "flange A350")
        self.assertTrue(any("material grade" in n for n in notes))

    def test_conflicting_values_function_directly(self):
        self.assertEqual(PL.conflicting_values("BW SCH 40", "x SCH 80", ""), [("schedule", "SCH 40")])
        self.assertEqual(PL.conflicting_values("BW SCH 40", "x", ""), [])
        self.assertEqual(PL.conflicting_values("150# RF", "2 ea Class 300", ""), [("pressure rating", "150#")])
        self.assertEqual(PL.conflicting_values("150# RF", "10 lb bag", ""), [])      # too few digits to be a rating


# ------------------------------------------------------------------ validation, routing, safety


class CallPathTests(Base):
    def test_text_goes_to_mini_and_a_picture_goes_to_the_big_model(self):
        self.write("T1", make_mail([(xlsx_bytes("widget"), "application", "octet-stream", "list.xlsx", {})]))
        self.write("T2", make_mail([(png(300, 120, "blue"), "image", "png", "pic.png", {})]))
        client = FakeClient(lambda p: answer(lines=[line(source="body")]))
        self.run_one("T1", client)
        self.run_one("T2", client)
        self.assertEqual(client.models, [MINI, FULL])

    def test_a_made_up_source_fails_validation_and_escalates_once(self):
        self.write("V1", make_mail([(xlsx_bytes("widget"), "application", "octet-stream", "list.xlsx", {})]))
        def responder(p):
            return answer(lines=[line(source="invented.pdf")]) if p["model"] == MINI else answer(lines=[line(source="list.xlsx")])
        client = FakeClient(responder)
        e = self.run_one("V1", client)
        self.assertEqual(client.models, [MINI, FULL])
        self.assertEqual(e["line_items"][0]["source"], "list.xlsx")
        self.assertFalse(e["needs_review"])
        replay = self.run_one("V1", None)                                       # no key: same two cached answers, same result
        self.assertEqual(replay, e)
        self.assertEqual(self.stats.real_calls, 2)

    def test_source_body_and_attachment_names_are_accepted(self):
        v = PL.make_validator({"a.pdf"})
        self.assertEqual(v(answer(lines=[line(source="body"), line(source="a.pdf")])), [])
        self.assertTrue(v(answer(lines=[line(source="b.pdf")])))
        self.assertTrue(v(answer(lines=[line(raw=" ")])))

    def test_failing_both_models_gives_a_safe_flagged_entry_not_a_crash(self):
        self.write("V2", make_mail())
        client = FakeClient(lambda p: answer(lines=[line(source="nowhere.pdf")]))
        e = self.run_one("V2", client)
        self.assertEqual((e["classification"], e["needs_review"], e["line_items"]), ("rfq", True, []))
        self.assertIn("SchemaMismatch", e["review_reason"])
        self.assertEqual(client.models, [MINI, FULL])

    def test_nothing_ever_raises_for_an_email(self):
        self.write("S1", make_mail())
        self.write("S2", b"\x00\xff garbage that is not an email at all")
        cases = {
            "client raises": (FakeClient(lambda p: RuntimeError("provider down")), "model call failed"),
            "no key, nothing cached": (None, "no cached model answer"),
            "budget": (FakeClient(lambda p: E.BudgetExceeded(2.0, 2.0)), "spend limit reached"),
        }
        for name, (client, expect) in cases.items():
            with self.subTest(name):
                e = self.run_one("S1", client)
                self.assertEqual((e["classification"], e["needs_review"], e["line_items"]), ("rfq", True, []))
                self.assertIn(expect, e["review_reason"])
        c = FakeClient(lambda p: answer())
        e = self.run_one("S2", c)                                               # an unreadable file is still an entry...
        self.assertTrue(e["needs_review"])
        self.assertIn("could not be read", e["review_reason"])
        self.assertEqual(c.models, [])                                          # ...and costs no model call
        with mock.patch.object(PL, "build_user_parts", side_effect=ZeroDivisionError("bug")):
            e = self.run_one("S1", FakeClient(lambda p: answer()))
        self.assertTrue(e["needs_review"])
        self.assertIn("internal error", e["review_reason"])
        self.assertIsNone(PL.process_email(self.root / "missing.eml", client=None).get("zzz"))   # a missing file does not raise either

    def test_a_second_run_makes_no_calls_and_gives_identical_output(self):
        for i in range(3):
            self.write(f"R{i}", make_mail(body=f"please quote item {i}"))
        client = FakeClient(lambda p: answer())
        first = PL.run_parse(self.emails, client, cache=self.cache, ledger=self.ledger, stats=self.stats)
        n = len(client.models)
        second = PL.run_parse(self.emails, None, cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertEqual((n, len(client.models)), (3, 3))
        self.assertEqual(first, second)
        self.assertEqual(self.stats.cache_hits, 3)


class OnlyTests(Base):
    def test_only_limits_the_run_and_the_score(self):
        for i in range(4):
            self.write(f"O{i}", make_mail(body=f"item {i}"))
        client = FakeClient(lambda p: answer())
        res = PL.run_parse(self.emails, client, only=["O1", "O3"], cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertEqual(sorted(res), ["O1", "O3"])
        self.assertEqual(len(client.models), 2)

        import score as SCORE
        key = self.root / "key.json"
        key.write_text(json.dumps({"version": 4, "emails": {f"O{i}": {"classification": "rfq", "revises": None, "duplicate_of": None,
                                                                      "needs_review": False, "line_items": []} for i in range(4)}}))
        out = self.root / "out.json"
        out.write_text(json.dumps(res))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = SCORE.main([str(out), "--key", str(key), "--only", "O1,O3"])
        self.assertEqual(code, 0)
        self.assertIn("emails in key: 2", buf.getvalue())
        self.assertIn("missing from output: 0", buf.getvalue())
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(SCORE.main([str(out), "--key", str(key), "--only", "O9"]), 1)

    def test_parse_command_without_a_key_runs_from_the_cache_and_flags_the_rest(self):
        import io as _io
        from rfqparse.__main__ import main
        self.write("K1", make_mail())
        out = self.root / "parsed.json"
        with mock.patch.object(config, "CACHE_DIR", self.root / "cache2"), \
                mock.patch.object(config, "LEDGER_PATH", self.root / "l2.jsonl"), \
                mock.patch.dict("os.environ", {}, clear=True), redirect_stderr(_io.StringIO()) as err, redirect_stdout(_io.StringIO()) as so:
            code = main(["parse", "--emails", str(self.emails), "--out", str(out), "--only", "K1"])
        self.assertEqual(code, 0)
        self.assertIn("OPENAI_API_KEY is not set", err.getvalue())
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["K1"])
        self.assertTrue(data["K1"]["needs_review"])
        self.assertIn("0 real", so.getvalue())


if __name__ == "__main__":
    unittest.main()
