"""End-to-end reliability tests: a hostile inbox, a provider that is down, no key, partial reads, CLI edge cases.
Fake client only; nothing touches the repo cache or ledger.

    python tests/test_chaos.py -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from rfqparse import __main__ as CLI, config, pipeline as PL  # noqa: E402
from rfqparse.llm import errors as E  # noqa: E402
from rfqparse.render import csv_ as CSV  # noqa: E402
from test_pipeline import FakeClient, answer, line  # noqa: E402
from test_registry import docx_bytes, make_mail, xlsx_bytes  # noqa: E402
from test_render_pdf import build_pdf, text_content  # noqa: E402

_spec = importlib.util.spec_from_file_location("check_format", ROOT / "data" / "check_format.py")
CF = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CF)


def locked_pdf() -> bytes:
    return build_pdf([{"content": text_content(["secret"])}], encrypt_user_pw="pw")


def many_page_pdf(n=300) -> bytes:
    return build_pdf([{"content": text_content([f"page {i} item 2 ea 1 in elbow"])} for i in range(1, n + 1)])


def att(data, mt, st, name):
    return [(data, mt, st, name, {})]


class FakeWithGuard(FakeClient):
    guard = SimpleNamespace(spent=0.0)


class Chaos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        good = make_mail(body="please quote 2 ea 1 inch elbow")
        good_bytes = good.as_bytes()
        files = {
            "N001": good_bytes,
            "Z000": b"",
            "R000": os.urandom(4096),
            "T000": make_mail(att(xlsx_bytes("widget"), "application", "octet-stream", "a.xlsx"), body="x" * 3000).as_bytes()[:900],
            "X000": make_mail(att(xlsx_bytes("widget")[:200], "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet", "bad.xlsx")).as_bytes(),
            "L000": make_mail(att(locked_pdf(), "application", "pdf", "locked.pdf")).as_bytes(),
            "B000": make_mail(att(b"", "application", "pdf", "empty.pdf")).as_bytes(),
            "P300": make_mail(att(many_page_pdf(), "application", "pdf", "big.pdf")).as_bytes(),
            "U000": make_mail(att(b"\x00\x01\x02\x03binary", "application", "x-weird", "thing.xyz")).as_bytes(),
        }
        for k, v in files.items():
            (self.inbox / f"{k}.eml").write_bytes(v)
        self.ids = sorted(files)
        self.cfg = mock.patch.multiple(config, CACHE_DIR=self.root / "cache", LEDGER_PATH=self.root / "ledger.jsonl")
        self.cfg.start()
        self.addCleanup(self.cfg.stop)

    def cli(self, client, extra=None, inbox=None, out=None):
        out = out or (self.root / "o" / "parsed.json")
        argv = ["parse", "--emails", str(inbox or self.inbox), "--out", str(out)] + (extra or [])
        err, so = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), redirect_stderr(err), redirect_stdout(so):
            if client is not None:
                with mock.patch("rfqparse.llm.client.make_client", return_value=client):
                    code = CLI.main(argv)
            else:
                code = CLI.main(argv)               # real make_client, key absent -> MissingKey
        return code, err.getvalue() + so.getvalue(), out

    def check_all(self, out):
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), self.ids, "every file has an entry")
        self.assertEqual(CF.check(data), [], "valid shape")
        return data

    def test_mixed_inbox_with_working_model(self):
        code, text, out = self.cli(FakeWithGuard(lambda p: answer(lines=[line()])))
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", text)
        d = self.check_all(out)
        self.assertFalse(d["N001"]["needs_review"])
        self.assertEqual(len(d["N001"]["line_items"]), 1)
        for k in ("Z000", "R000", "X000", "L000", "B000", "P300", "U000"):
            self.assertTrue(d[k]["needs_review"], k)
            self.assertTrue(d[k]["review_reason"], k)
        self.assertIn("only the first", d["P300"]["review_reason"])
        self.assertEqual(len(d["P300"]["line_items"]), 1)          # the lines that were read are still returned
        self.assertIn("could not be read", d["X000"]["review_reason"])
        self.assertIn("could not be read", d["L000"]["review_reason"])
        self.assertIn("could not be read", d["B000"]["review_reason"])
        self.assertIn("could not be read", d["U000"]["review_reason"])
        self.assertTrue(d["Z000"]["review_reason"] and d["R000"]["review_reason"])

    def test_provider_down(self):
        down = E.ApiFailure("server: HTTP 500", kind="server", status_code=500, attempts=3)
        code, text, out = self.cli(FakeWithGuard(lambda p: down))
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", text)
        d = self.check_all(out)
        for k, v in d.items():
            self.assertTrue(v["needs_review"] and v["review_reason"], k)
            self.assertEqual(v["line_items"], [], k)

    def test_no_key_and_empty_cache(self):
        code, text, out = self.cli(None)
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", text)
        self.assertIn("OPENAI_API_KEY is not set", text)
        d = self.check_all(out)
        for k, v in d.items():
            self.assertTrue(v["needs_review"] and v["review_reason"], k)

    def test_budget_tripped_mid_run(self):
        def responder(p):
            return answer(lines=[line()])
        c = FakeWithGuard(responder)
        orig = c.complete

        def boom(payload):
            raise E.BudgetExceeded(2.0, 2.0, "m")
        c.complete = boom
        code, text, out = self.cli(c)
        self.assertEqual(code, 0)
        d = self.check_all(out)
        self.assertTrue(all(v["needs_review"] for v in d.values()))
        self.assertTrue(any("spend limit" in (v["review_reason"] or "") for v in d.values()))

    def test_second_run_is_all_cache(self):
        c = FakeWithGuard(lambda p: answer(lines=[line()]))
        self.cli(c)
        n = len(c.models)
        code, text, out = self.cli(FakeWithGuard(lambda p: self.fail("no call expected")))
        self.assertEqual(code, 0)
        self.assertGreater(n, 0)
        d = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(d["N001"]["needs_review"])


class CliEdges(Chaos):
    def test_missing_emails_folder_errors_and_writes_nothing(self):
        out = self.root / "x" / "parsed.json"
        code, text, _ = self.cli(None, inbox=self.root / "nope", out=out)
        self.assertEqual(code, 2)
        self.assertIn("not found", text)
        self.assertFalse(out.exists())

    def test_emails_path_is_a_file(self):
        code, text, _ = self.cli(None, inbox=self.inbox / "N001.eml")
        self.assertEqual(code, 2)

    def test_out_parent_created(self):
        out = self.root / "a" / "b" / "c" / "parsed.json"
        code, _, _ = self.cli(FakeWithGuard(lambda p: answer()), out=out)
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())

    def test_empty_folder_exits_zero_with_empty_object(self):
        empty = self.root / "empty"
        empty.mkdir()
        code, _, out = self.cli(FakeWithGuard(lambda p: answer()), inbox=empty)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {})

    def test_run_failure_inside_still_writes_every_file_and_exits_zero(self):
        with mock.patch.object(PL, "run_parse", side_effect=RuntimeError("boom")):
            code, text, out = self.cli(FakeWithGuard(lambda p: answer()))
        self.assertEqual(code, 0)
        d = self.check_all(out)
        self.assertTrue(all(v["needs_review"] for v in d.values()))

    def test_uppercase_extension_is_read(self):
        (self.inbox / "N001.eml").rename(self.inbox / "N001.EML")
        code, _, out = self.cli(FakeWithGuard(lambda p: answer(lines=[line()])))
        self.assertIn("N001", json.loads(out.read_text(encoding="utf-8")))


class PartialReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "e").mkdir()
        self.client = FakeClient(lambda p: answer(lines=[line()]))

    def run_mail(self, msg):
        (self.root / "e" / "P1.eml").write_bytes(msg.as_bytes())
        from rfqparse.llm import cache as C, ledger as L
        res = PL.run_parse(self.root / "e", self.client, cache=C.Cache(self.root / "c"), ledger=L.Ledger(self.root / "l"))
        return res["P1"]

    def test_pdf_over_page_cap(self):
        e = self.run_mail(make_mail(att(many_page_pdf(20), "application", "pdf", "p.pdf")))
        self.assertTrue(e["needs_review"])
        self.assertIn(f"only the first {config.PDF_MAX_PAGES}", e["review_reason"])
        self.assertIn("partly read", e["review_reason"])
        self.assertEqual(len(e["line_items"]), 1)

    def test_pdf_under_cap_is_not_flagged(self):
        e = self.run_mail(make_mail(att(many_page_pdf(3), "application", "pdf", "p.pdf")))
        self.assertFalse(e["needs_review"])

    def test_csv_cut_by_size_cap(self):
        rows = "\n".join(f"{i},elbow,2" for i in range(30)).encode()
        with mock.patch.object(CSV, "MAX_ROWS", 5):
            e = self.run_mail(make_mail(att(rows, "text", "csv", "l.csv")))
        self.assertTrue(e["needs_review"])
        self.assertIn("partly read", e["review_reason"])
        self.assertEqual(len(e["line_items"]), 1)

    def test_xlsx_cut_by_row_cap(self):
        from openpyxl import Workbook
        from rfqparse.render import xlsx as X
        wb = Workbook()
        for i in range(30):
            wb.active.append([f"row{i}", 2])
        b = io.BytesIO()
        wb.save(b)
        with mock.patch.object(X, "MAX_ROWS_PER_SHEET", 5):
            e = self.run_mail(make_mail(att(b.getvalue(), "application", "octet-stream", "l.xlsx")))
        self.assertTrue(e["needs_review"])
        self.assertIn("partly read", e["review_reason"])

    def test_docx_with_unreadable_part(self):
        src = zipfile.ZipFile(io.BytesIO(docx_bytes("2 ea elbow")))
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for n in src.namelist():
                z.writestr(n, src.read(n))
            z.writestr("word/header1.xml", "<not xml")
        e = self.run_mail(make_mail(att(b.getvalue(), "application", "octet-stream", "d.docx")))
        self.assertTrue(e["needs_review"])
        self.assertIn("header1.xml unreadable", e["review_reason"])
        self.assertEqual(len(e["line_items"]), 1)

    def test_fully_read_attachments_not_flagged(self):
        e = self.run_mail(make_mail(att(xlsx_bytes("elbow"), "application", "octet-stream", "ok.xlsx")))
        self.assertFalse(e["needs_review"])


if __name__ == "__main__":
    unittest.main()
