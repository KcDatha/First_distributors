"""Tests for the completeness check and second pass (rfqparse/postprocess/completeness.py + pipeline.second_pass),
with a fake client and synthetic spreadsheets. No real calls.

    python tests/test_completeness.py -v
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook
from openpyxl.styles import Font

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from rfqparse import config, pipeline as PL  # noqa: E402
from rfqparse.llm import cache as C  # noqa: E402
from rfqparse.llm import ledger as L  # noqa: E402
from rfqparse.llm.call import CallStats  # noqa: E402
from rfqparse.llm.prompts import system_prompt  # noqa: E402
from rfqparse.postprocess import completeness as CP  # noqa: E402
from test_pipeline import FakeClient, answer, line  # noqa: E402
from test_registry import make_mail  # noqa: E402

MINI = "gpt-4.1-mini"
NAMES = ["alpha valve", "bravo elbow", "charlie tee", "delta cap", "echo union", "foxtrot nipple", "golf plug", "hotel bushing"]
ITEMS = [(i + 1, 3 + i, '2"', n) for i, n in enumerate(NAMES)]            # (item no, qty, size, name)


def row_text(it):
    return f"{it[0]} | {it[1]} | EA | {it[2]} | {it[3]}"


def item_line(it, source="list.xlsx"):
    return line(raw=row_text(it), desc=f"{it[2]} {it[3]}", qty=it[1], uom="EA", source=source)


def xlsx(items=ITEMS, total=True, extra=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Shutdown materials list"])
    ws.append(["ITEM", "QTY", "UOM", "SIZE", "DESCRIPTION"])
    for it in items:
        ws.append([it[0], it[1], "EA", it[2], it[3]])
    if total:
        ws.append([None, "TOTAL QTY", sum(i[1] for i in items)])
    if extra:
        extra(wb, ws)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def responder_factory(first, second=None, third=None):
    """first(payload) for the first pass, second(payload) for the second pass."""
    def responder(p):
        if "SECOND PASS" in p["system"]:
            return second(p) if second else answer(lines=[])
        return first(p)
    return responder


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.emails = self.root / "emails"
        self.emails.mkdir()
        self.cache, self.ledger, self.stats = C.Cache(self.root / "cache"), L.Ledger(self.root / "l.jsonl"), CallStats()

    def write(self, eid="C1", data=None, subject=None, **kw):
        m = make_mail([(data if data is not None else xlsx(), "application", "octet-stream", "list.xlsx", {})], **kw)
        if subject:
            del m["Subject"]
            m["Subject"] = subject
        (self.emails / f"{eid}.eml").write_bytes(m.as_bytes())

    def run_one(self, client, eid="C1", notes=None):
        return PL.process_email(self.emails / f"{eid}.eml", client=client, cache=self.cache, ledger=self.ledger,
                                stats=self.stats, notes=notes)

    def pkg(self, data):
        from rfqparse.ingest import ingest_eml
        from rfqparse.render.registry import render_email
        self.write("P1", data)
        return render_email(ingest_eml(self.emails / "P1.eml"))


class SecondPassTests(Base):
    def test_a_first_answer_missing_the_last_rows_is_filled_by_the_second_pass(self):
        self.write()
        seen = {}
        def second(p):
            seen["system"], seen["last"] = p["system"], p["user"][-1]["text"]
            seen["first_part"] = p["user"][0]["text"]
            return answer(lines=[item_line(it) for it in ITEMS[5:]])
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]), second))
        notes = []
        e = self.run_one(client, notes=notes)
        self.assertEqual(client.models, [MINI, MINI])                                  # first pass, then one second pass
        self.assertEqual(len(e["line_items"]), 8)
        self.assertEqual(sorted(l["raw"] for l in e["line_items"]), sorted(row_text(i) for i in ITEMS))
        self.assertFalse(e["needs_review"])
        # the second pass got the whole original email plus exactly the three unaccounted rows
        self.assertIn("=== CURRENT MESSAGE ===", seen["first_part"])
        self.assertIn("=== ATTACHMENT: list.xlsx", seen["first_part"])
        listed = [l for l in seen["last"].splitlines() if l.startswith("- [")]
        self.assertEqual(len(listed), 3)
        for it in ITEMS[5:]:
            self.assertTrue(any(it[3] in l for l in listed))
        self.assertIn("classification (as already decided): rfq", seen["last"])
        self.assertTrue(any("second pass" in n and "added 3 line(s)" in n for n in notes))

    def test_the_second_pass_has_its_own_prompt_and_version_and_never_touches_first_pass_keys(self):
        self.write()
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: answer(lines=[item_line(it) for it in ITEMS[5:]])))
        self.run_one(client)
        entries, _ = self.cache.entries()
        versions = sorted(e.prompt_version for e in entries)
        self.assertEqual(versions, sorted([config.PROMPT_VERSION, config.COMPLETENESS_PROMPT_VERSION]))
        self.assertEqual(config.COMPLETENESS_PROMPT_VERSION, "completeness-v1")
        self.assertNotEqual(config.COMPLETENESS_PROMPT_VERSION, config.PROMPT_VERSION)
        self.assertTrue(CP.SECOND_PASS_ADDENDUM.strip().startswith("SECOND PASS"))
        # the first-pass key is exactly what it was before this feature existed: version v3, plain system prompt
        first = [e for e in entries if e.prompt_version == config.PROMPT_VERSION][0]
        second = [e for e in entries if e.prompt_version == config.COMPLETENESS_PROMPT_VERSION][0]
        self.assertNotEqual(first.key, second.key)
        self.assertTrue(PL.CP_system().startswith(system_prompt()))
        self.assertNotEqual(PL.CP_system(), system_prompt())

    def test_a_complete_first_answer_makes_no_second_call(self):
        self.write()
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS])))
        e = self.run_one(client)
        self.assertEqual(client.models, [MINI])
        self.assertEqual((len(e["line_items"]), e["needs_review"]), (8, False))

    def test_a_total_row_does_not_trigger_a_call(self):
        data = xlsx()
        self.assertIn("TOTAL QTY", [a for a in self.pkg(data).attachments][0].text)       # the total row IS in what we render
        self.assertEqual(len(CP.candidate_rows(self.pkg(data))), 8)                        # ...but is not a candidate item row
        self.write(data=data)
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS])))
        self.run_one(client)
        self.assertEqual(client.models, [MINI])

    def test_a_second_pass_that_still_misses_is_flagged_for_review_with_the_count(self):
        self.write()
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: answer(lines=[item_line(ITEMS[5])])))
        e = self.run_one(client)
        self.assertEqual(len(e["line_items"]), 6)                                          # what could be read is kept
        self.assertTrue(e["needs_review"])
        self.assertIn("2 of 8 table rows could not be accounted for after a second pass", e["review_reason"])
        self.assertIn("golf plug", e["review_reason"])                                     # names an example row a human can find

    def test_a_second_pass_line_for_a_total_row_is_dropped(self):
        self.write()
        total = line(raw="| TOTAL QTY | 36", desc="total quantity", qty=36, uom=None, source="list.xlsx")
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: answer(lines=[item_line(it) for it in ITEMS[5:]] + [total])))
        notes = []
        e = self.run_one(client, notes=notes)
        self.assertEqual(len(e["line_items"]), 8)
        self.assertFalse(any("TOTAL" in l["raw"] for l in e["line_items"]))
        self.assertFalse(e["needs_review"])
        self.assertTrue(any("dropped second-pass line" in n and "TOTAL" in n for n in notes))

    def test_lines_already_returned_are_not_duplicated(self):
        self.write()
        everything = [item_line(it) for it in ITEMS]                                      # the second pass repeats the lot
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: answer(lines=everything)))
        e = self.run_one(client)
        raws = [l["raw"] for l in e["line_items"]]
        self.assertEqual(len(raws), 8)
        self.assertEqual(len(set(raws)), 8)

    def test_an_invented_line_from_the_second_pass_is_dropped(self):
        self.write()
        fake = line(raw="99 | 7 | EA | 1/2 | invented gadget", desc="gadget", qty=7, source="list.xlsx")
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: answer(lines=[item_line(it) for it in ITEMS[5:]] + [fake])))
        e = self.run_one(client)
        self.assertEqual(len(e["line_items"]), 8)
        self.assertNotIn("invented", " ".join(l["raw"] for l in e["line_items"]))

    def test_second_pass_lines_go_through_the_same_guards(self):
        self.write(subject="Shutdown materials list")
        def second(p):
            ls = [item_line(it) for it in ITEMS[5:]]
            ls[0] = dict(ls[0], applies_context=["Shutdown materials list"], qty=None)
            return answer(lines=ls)
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]), second))
        e = self.run_one(client)
        new = [l for l in e["line_items"] if "foxtrot" in l["raw"]][0]
        self.assertTrue(new["description"].endswith("Shutdown materials list"))           # context guard applied
        self.assertEqual(sorted(l["raw"] for l in e["line_items"] if l["source"] == "list.xlsx").__len__(), 8)

    def test_second_pass_answers_are_validated_like_the_first(self):
        self.write()
        # a bad source AND a raw that appears nowhere in the email: the source repair cannot help, so validation fails
        bad = [dict(item_line(it), source="nowhere.pdf", raw=row_text(it) + " zzz paraphrase") for it in ITEMS[5:]]
        def second(p):
            return answer(lines=bad if p["model"] == MINI else [item_line(it) for it in ITEMS[5:]])
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]), second))
        e = self.run_one(client)
        self.assertEqual(client.models, [MINI, MINI, "gpt-4.1"])                          # the usual one-shot escalation
        self.assertEqual(len(e["line_items"]), 8)

    def test_non_request_classes_are_not_checked(self):
        self.write()
        client = FakeClient(lambda p: answer(cls="vendor_quote", lines=[]))
        e = self.run_one(client)
        self.assertEqual(client.models, [MINI])
        self.assertEqual((e["classification"], e["needs_review"], e["line_items"]), ("vendor_quote", False, []))

    def test_a_replay_with_no_key_takes_the_same_path_from_the_cache(self):
        self.write()
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: answer(lines=[item_line(it) for it in ITEMS[5:]])))
        first = self.run_one(client)
        calls = (self.stats.real_calls, len(client.models))
        replay = self.run_one(None)
        self.assertEqual(replay, first)
        self.assertEqual((self.stats.real_calls, len(client.models)), calls)               # no new calls
        self.assertEqual(self.stats.cache_hits, 2)                                         # both passes came from the cache

    def test_a_failed_second_pass_flags_the_email_and_keeps_the_first_lines(self):
        self.write()
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: RuntimeError("provider down")))
        notes = []
        e = self.run_one(client, notes=notes)
        self.assertEqual(len(e["line_items"]), 5)
        self.assertTrue(e["needs_review"])
        self.assertIn("3 table row(s) have no line and the second pass could not run", e["review_reason"])
        self.assertTrue(any("second pass failed" in n for n in notes))

    def test_no_key_and_nothing_cached_for_the_second_pass_flags_the_email(self):
        self.write()
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: RuntimeError("down")))
        self.run_one(client)                                                               # first pass cached, second not
        e = self.run_one(None)
        self.assertEqual(len(e["line_items"]), 5)
        self.assertTrue(e["needs_review"])
        self.assertIn("could not run", e["review_reason"])

    def test_rows_beyond_the_ask_limit_are_flagged_not_silently_ignored(self):
        self.write()
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(ITEMS[0])]),
                                              lambda p: answer(lines=[item_line(it) for it in ITEMS[1:3]])))
        with mock.patch.object(config, "COMPLETENESS_MAX_ROWS_ASKED", 2):
            e = self.run_one(client)
        self.assertEqual(len(e["line_items"]), 3)
        self.assertTrue(e["needs_review"])
        self.assertIn("5 of 8 table rows could not be accounted for", e["review_reason"])


class SecondPassSourceRepairTests(Base):
    def test_second_pass_lines_get_the_same_source_repair(self):
        self.write()
        bad = [dict(item_line(it), source="the spreadsheet") for it in ITEMS[5:]]        # right text, invented source name
        client = FakeClient(responder_factory(lambda p: answer(lines=[item_line(it) for it in ITEMS[:5]]),
                                              lambda p: answer(lines=bad)))
        notes = []
        e = self.run_one(client, notes=notes)
        self.assertEqual(client.models, [MINI, MINI])                                  # repaired, so no escalation
        self.assertEqual(len(e["line_items"]), 8)
        self.assertTrue(all(l["source"] == "list.xlsx" for l in e["line_items"]))
        self.assertEqual(sum("source repaired" in n for n in notes), 3)


class CandidateRowTests(Base):
    def rows(self, **kw):
        return CP.candidate_rows(self.pkg(xlsx(**kw)))

    def test_item_rows_only_not_headings_headers_or_totals(self):
        rows = self.rows()
        self.assertEqual([r.number for r in rows], list(range(3, 11)))
        self.assertTrue(all("TOTAL" not in r.text for r in rows))

    def test_total_wording_variants_are_skipped(self):
        def extra(wb, ws):
            for label in ("Subtotal", "GRAND TOTAL", "Freight", "Sales tax", "Balance due", "sum"):
                ws.append([None, label, 123])
        self.assertEqual(len(self.rows(extra=extra)), 8)

    def test_a_word_that_merely_contains_a_total_word_is_not_a_total_row(self):
        def extra(wb, ws):
            ws.append([9, 2, "EA", '3"', "totalizer flow meter, taxonomy tag"])
        rows = self.rows(extra=extra)
        self.assertEqual(len(rows), 9)                                                    # "totalizer"/"taxonomy" are not total words

    def test_section_heading_rows_with_one_cell_are_not_candidates(self):
        def extra(wb, ws):
            ws.append(["90 ELBOWS"])
            ws.append(["Notes: 2 pallets, 3 boxes"])
        self.assertEqual(len(self.rows(extra=extra)), 8)

    def test_hidden_sheets_hidden_rows_and_struck_rows_are_not_expected(self):
        def extra(wb, ws):
            old = wb.create_sheet("Old")
            old.append([1, 4, "EA", '2"', "superseded valve"])
            old.append([2, 5, "EA", '2"', "superseded elbow"])
            old.sheet_state = "hidden"
            ws.append([20, 1, "EA", '2"', "struck item"])
            ws.cell(ws.max_row, 5).font = Font(strike=True)
            for c in range(1, 6):
                ws.cell(ws.max_row, c).font = Font(strike=True)
            ws.append([21, 1, "EA", '2"', "hidden row item"])
            ws.row_dimensions[ws.max_row].hidden = True
        rows = self.rows(extra=extra)
        texts = " ".join(r.text for r in rows)
        for gone in ("superseded", "struck item", "hidden row item"):
            self.assertNotIn(gone, texts)
        self.assertEqual(len(rows), 8)

    def test_csv_and_docx_rows_count_too(self):
        from rfqparse.ingest import ingest_eml
        from rfqparse.render.registry import render_email
        m = make_mail([(b"qty,size,item\n4,2in,alpha valve\n6,3in,bravo elbow\n", "text", "csv", "l.csv", {})])
        (self.emails / "X1.eml").write_bytes(m.as_bytes())
        rows = CP.candidate_rows(render_email(ingest_eml(self.emails / "X1.eml")))
        self.assertEqual([r.text for r in rows], ["4 | 2in | alpha valve", "6 | 3in | bravo elbow"])

    def test_rows_of_unreadable_or_picture_attachments_are_ignored(self):
        from rfqparse.ingest import ingest_eml
        from rfqparse.render.registry import render_email
        m = make_mail([(b"\xd0\xcf\x11\xe0" + b"\x00" * 40, "application", "vnd.ms-excel", "old.xls", {})])
        (self.emails / "X2.eml").write_bytes(m.as_bytes())
        self.assertEqual(CP.candidate_rows(render_email(ingest_eml(self.emails / "X2.eml"))), [])


class MatchingTests(unittest.TestCase):
    def rows(self, *texts):
        return [CP.TableRow("f", "s", i + 1, t) for i, t in enumerate(texts)]

    def test_a_line_accounts_for_its_row_even_when_the_raw_drops_a_column_or_reorders(self):
        rows = self.rows('34 | 10 | EA | 2" | BLIND FLANGE')
        for raw in ('10 | EA | 2" | BLIND FLANGE', 'BLIND FLANGE 2" 10 EA 34', '34 | 10 | EA | 2" | BLIND FLANGE'):
            self.assertEqual(CP.unaccounted(rows, [raw]), [], raw)

    def test_unrelated_text_does_not_account_for_a_row(self):
        rows = self.rows('34 | 10 | EA | 2" | BLIND FLANGE')
        self.assertEqual(len(CP.unaccounted(rows, ["12 ft of 3/4 galvanized pipe"])), 1)
        self.assertEqual(len(CP.unaccounted(rows, [])), 1)

    def test_one_line_covers_one_row_so_two_identical_rows_need_two_lines(self):
        rows = self.rows("2 | EA | 6in | cap", "2 | EA | 6in | cap")
        self.assertEqual(len(CP.unaccounted(rows, ["2 | EA | 6in | cap"])), 1)
        self.assertEqual(CP.unaccounted(rows, ["2 | EA | 6in | cap", "2 | EA | 6in | cap"]), [])

    def test_a_neighbouring_line_does_not_steal_a_row(self):
        rows = self.rows("33 | 5 | EA | 1-1/2 | BLIND FLANGE", "34 | 10 | EA | 2 | BLIND FLANGE")
        left = CP.unaccounted(rows, ["33 | 5 | EA | 1-1/2 | BLIND FLANGE"])
        self.assertEqual([r.number for r in left], [2])

    def test_the_threshold_is_in_config(self):
        rows = self.rows("a b c d")
        self.assertEqual(CP.unaccounted(rows, ["a b x y"]), rows)                         # 2/6 overlap
        with mock.patch.object(config, "COMPLETENESS_MATCH_THRESHOLD", 0.3):
            self.assertEqual(CP.unaccounted(rows, ["a b x y"]), [])


if __name__ == "__main__":
    unittest.main()
