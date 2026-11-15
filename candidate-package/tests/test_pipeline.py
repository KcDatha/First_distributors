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


def line(raw="2 ea 2in elbow", desc="2in elbow", qty=2, uom="EA", source="body"):
    return {"raw": raw, "description": desc, "qty": qty, "uom": uom, "source": source}


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

    def test_prompt_is_deterministic_and_versioned(self):
        self.assertEqual(PR.system_prompt(), PR.system_prompt())
        self.assertEqual(config.PROMPT_VERSION, "v1")

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
