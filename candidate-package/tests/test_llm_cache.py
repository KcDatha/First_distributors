"""Tests for rfqparse.llm (cache, ledger, get_or_call, cost report) using a fake client. No network.

    python tests/test_llm_cache.py -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rfqparse import config  # noqa: E402
from rfqparse.llm import cache as C  # noqa: E402
from rfqparse.llm import call as K  # noqa: E402
from rfqparse.llm import ledger as L  # noqa: E402

MODEL, PV, SV = "fake-model", "p1", "s1"
PRICES = {"fake-model": {"input_per_1m": 10.0, "output_per_1m": 20.0}}
IMG = bytes(range(256)) * 8


class FakeClient:
    """Counts calls. Tokens depend only on the payload so runs are repeatable."""

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def complete(self, payload):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider is down")
        n = len(json.dumps(C.canonical(payload)))
        return {"response": {"text": f"answer for {n}", "choices": [{"finish_reason": "stop"}]},
                "usage": {"prompt_tokens": 100 + n, "completion_tokens": 20}}


PAYLOADS = [{"email": f"E{i:03d}", "text": f"please quote item {i}", "images": [{"mime": "image/png", "data": IMG + bytes([i])}]}
            for i in range(5)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = C.Cache(self.root / "cache")
        self.ledger = L.Ledger(self.root / "ledger.jsonl")
        self.stats = K.CallStats()
        for p in (mock.patch.object(config, "MODEL_PRICES", PRICES),
                  mock.patch.object(config, "PRICES_ARE_PLACEHOLDER", True)):
            p.start()
            self.addCleanup(p.stop)

    def run_all(self, client, payloads=PAYLOADS, pv=PV, model=MODEL):
        return [K.get_or_call(p, client, model=model, prompt_version=pv, schema_version=SV, email_id=p["email"],
                              cache=self.cache, ledger=self.ledger, stats=self.stats) for p in payloads]


class RunTests(Base):
    def test_first_run_makes_n_calls_second_run_makes_none(self):
        client = FakeClient()
        first = self.run_all(client)
        self.assertEqual((client.calls, self.stats.real_calls, self.stats.cache_hits), (5, 5, 0))
        self.assertTrue(all(not r.cache_hit for r in first))
        second = self.run_all(client)
        self.assertEqual((client.calls, self.stats.real_calls, self.stats.cache_hits), (5, 5, 5))   # no new calls
        self.assertTrue(all(r.cache_hit for r in second))
        self.assertEqual([r.response for r in first], [r.response for r in second])
        self.assertEqual([r.key for r in first], [r.key for r in second])

    def test_second_run_works_with_no_client_at_all(self):
        self.run_all(FakeClient())
        again = self.run_all(None)                                   # no key, no client: all hits
        self.assertTrue(all(r.cache_hit for r in again))

    def test_changed_prompt_version_schema_model_or_payload_misses(self):
        client = FakeClient()
        self.run_all(client)
        self.run_all(client, pv="p2")
        self.assertEqual(client.calls, 10)
        self.run_all(client, model="other-model")
        self.assertEqual(client.calls, 15)
        K.get_or_call(PAYLOADS[0], client, model=MODEL, prompt_version=PV, schema_version="s2", email_id="E000",
                      cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertEqual(client.calls, 16)
        changed = dict(PAYLOADS[0], text="please quote item 0 (edited)")
        K.get_or_call(changed, client, model=MODEL, prompt_version=PV, schema_version=SV, email_id="E000",
                      cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertEqual(client.calls, 17)

    def test_miss_with_no_client_raises_typed_cachemiss(self):
        with self.assertRaises(C.CacheMiss) as cm:
            self.run_all(None)
        self.assertEqual(cm.exception.model, MODEL)
        self.assertEqual(len(cm.exception.key), 64)
        self.assertEqual(self.stats.misses_without_client, 1)
        self.assertEqual(self.ledger.rows()[0], [])                 # nothing was looked up successfully

    def test_failed_client_call_is_typed_and_not_cached(self):
        with self.assertRaises(K.LLMCallError) as cm:
            self.run_all(FakeClient(fail=True))
        self.assertIn("provider is down", str(cm.exception))
        self.assertEqual(self.cache.entries()[0], [])
        self.assertEqual((self.stats.real_calls, self.stats.cache_hits), (0, 0))

    def test_malformed_client_result_is_rejected(self):
        class Bad:
            def complete(self, payload):
                return {"response": "x"}                            # no usage
        with self.assertRaises(K.LLMCallError):
            self.run_all(Bad())
        self.assertEqual(self.cache.entries()[0], [])

    def test_cache_write_failure_still_returns_the_answer(self):
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            r = self.run_all(FakeClient(), PAYLOADS[:1])[0]
        self.assertEqual(self.stats.cache_write_errors, 1)
        self.assertIn("answer for", r.response["text"])
        self.assertEqual([p.name for p in self.cache.dir.glob("*")], [])      # no entry and no stray temp file


class KeyTests(unittest.TestCase):
    def test_same_key_regardless_of_dict_order(self):
        a = {"x": 1, "y": {"b": [1, 2, {"q": 1, "p": 2}], "a": "t"}, "z": None}
        b = {"z": None, "y": {"a": "t", "b": [1, 2, {"p": 2, "q": 1}]}, "x": 1}
        self.assertEqual(C.make_key(MODEL, PV, SV, a), C.make_key(MODEL, PV, SV, b))
        self.assertNotEqual(C.make_key(MODEL, PV, SV, {"l": [1, 2]}), C.make_key(MODEL, PV, SV, {"l": [2, 1]}))  # list order matters

    def test_key_does_not_depend_on_where_the_folder_lives(self):
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            c1 = C.Cache(Path(t1) / "a" / "b" / "cache")
            c2 = C.Cache(Path(t2) / "totally" / "different" / "place")
            for c in (c1, c2):
                K.get_or_call({"email": "E1", "text": "hi"}, FakeClient(), model=MODEL, prompt_version=PV, schema_version=SV,
                              email_id="E1", cache=c, ledger=L.Ledger(Path(t1) / "l.jsonl"), stats=K.CallStats())
            self.assertEqual([p.name for p in c1.dir.glob("*.json")], [p.name for p in c2.dir.glob("*.json")])

    def test_key_does_not_depend_on_the_cwd_and_default_dir_is_found_from_the_package(self):
        key = C.make_key(MODEL, PV, SV, {"a": 1})
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as t:
            os.chdir(t)
            try:
                self.assertEqual(C.make_key(MODEL, PV, SV, {"a": 1}), key)
                self.assertEqual(C.default_cache_dir(), Path(C.__file__).resolve().parents[2] / "cache")
                self.assertTrue(C.Cache().dir.is_absolute())
                self.assertEqual(C.Cache().dir.parent, Path(__file__).resolve().parent.parent)   # repo root, not the temp cwd
            finally:
                os.chdir(here)

    def test_images_enter_the_key_as_their_own_sha256_not_base64(self):
        text = C.canonical_json(MODEL, PV, SV, {"img": IMG})
        self.assertIn(hashlib.sha256(IMG).hexdigest(), text)
        self.assertNotIn(base64.b64encode(IMG)[:40].decode(), text)
        self.assertEqual(C.make_key(MODEL, PV, SV, {"img": IMG}), C.make_key(MODEL, PV, SV, {"img": bytes(IMG)}))
        self.assertNotEqual(C.make_key(MODEL, PV, SV, {"img": IMG}), C.make_key(MODEL, PV, SV, {"img": IMG + b"\x00"}))

    def test_machine_dependent_or_unknown_values_are_refused(self):
        for bad in (Path("/Users/someone/file.png"), {"p": Path("C:/x/y.png")}, {"s": {1, 2}}, {1: "int key"},
                    float("nan"), object()):
            with self.subTest(repr(bad)[:30]), self.assertRaises((TypeError, ValueError)):
                C.make_key(MODEL, PV, SV, bad)


class GenerationSettingsKeyTests(Base):
    def test_settings_are_part_of_the_key(self):
        base = C.make_key(MODEL, PV, SV, {"a": 1})
        with mock.patch.object(config, "MODEL_SETTINGS", {MODEL: {"temperature": 0, "max_completion_tokens": 123}}):
            smaller = C.make_key(MODEL, PV, SV, {"a": 1})
            same_again = C.make_key(MODEL, PV, SV, {"a": 1})
        with mock.patch.object(config, "MODEL_SETTINGS", {MODEL: {"temperature": 1, "max_completion_tokens": 123}}):
            warmer = C.make_key(MODEL, PV, SV, {"a": 1})
        with mock.patch.object(config, "MODEL_SETTINGS", {MODEL: {"temperature": None, "max_completion_tokens": 123}}):
            no_temp = C.make_key(MODEL, PV, SV, {"a": 1})
        self.assertEqual(smaller, same_again)
        self.assertEqual(len({base, smaller, warmer, no_temp}), 4)

    def test_a_changed_output_limit_misses_instead_of_serving_the_old_reply(self):
        client = FakeClient()
        self.run_all(client, PAYLOADS[:2])
        self.run_all(client, PAYLOADS[:2])
        self.assertEqual((client.calls, self.stats.cache_hits), (2, 2))
        with mock.patch.object(config, "MODEL_SETTINGS", {MODEL: {"temperature": 0, "max_completion_tokens": 99999}}):
            self.run_all(client, PAYLOADS[:2])
        self.assertEqual(client.calls, 4)                                         # new limit, new calls
        entries, _ = self.cache.entries()
        self.assertEqual({e.generation["max_output_tokens"] for e in entries}, {config.DEFAULT_MODEL_SETTINGS["max_completion_tokens"], 99999})

    def test_explicit_settings_override_and_appear_in_the_canonical_json(self):
        text = C.canonical_json(MODEL, PV, SV, {"a": 1}, {"temperature": 0, "max_output_tokens": 77})
        self.assertIn('"max_output_tokens":77', text)
        self.assertIn('"temperature":0', text)


class EntryTests(Base):
    def test_entry_file_has_only_the_expected_fields_and_no_secrets(self):
        class Leaky(FakeClient):
            def complete(self, payload):
                out = super().complete(payload)
                out["response"]["headers"] = {"Authorization": "Bearer sk-secret"}
                out["response"]["api_key"] = "sk-secret"
                out["usage"]["headers"] = {"x": 1}
                return out
        r = self.run_all(Leaky(), PAYLOADS[:1])[0]
        text = self.cache.path_for(r.key).read_text(encoding="utf-8")
        d = json.loads(text)
        self.assertEqual(sorted(d), ["created_at", "email_id", "generation", "key", "latency_s", "model",
                                     "prompt_version", "response", "schema_version", "usage"])
        self.assertEqual(d["generation"], C.generation_settings(MODEL))
        self.assertNotIn("sk-secret", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn(str(self.root), text)                      # no absolute paths
        self.assertEqual((d["model"], d["prompt_version"], d["email_id"]), (MODEL, PV, "E000"))
        self.assertEqual(d["usage"]["completion_tokens"], 20)

    def test_write_is_atomic_no_temp_files_left(self):
        self.run_all(FakeClient())
        names = [p.name for p in self.cache.dir.iterdir()]
        self.assertEqual(len(names), 5)
        self.assertTrue(all(n.endswith(".json") and not n.startswith(".") for n in names))

    def test_corrupt_or_unreadable_entries_are_misses_and_get_repaired(self):
        client = FakeClient()
        keys = [r.key for r in self.run_all(client)]
        self.assertEqual(client.calls, 5)
        bad_versions = {
            0: b"{ this is not json",
            1: b"",
            2: json.dumps({"key": keys[2], "model": MODEL}).encode(),                       # right shape missing fields
            3: json.dumps({"key": "someone-else", "model": MODEL, "prompt_version": PV,
                           "response": {}, "usage": {}}).encode(),                           # key does not match the file
            4: b"\xff\xfe\x00 binary junk",
        }
        for i, blob in bad_versions.items():
            self.cache.path_for(keys[i]).write_bytes(blob)
            self.assertIsNone(self.cache.get(keys[i]), f"entry {i} should be a miss")
        self.assertEqual(self.cache.corrupt_seen, 5)
        again = self.run_all(client)                                                          # no crash, five fresh calls
        self.assertEqual(client.calls, 10)
        self.assertTrue(all(not r.cache_hit for r in again))
        self.assertTrue(all(self.cache.get(k) is not None for k in keys))                    # repaired
        self.run_all(client)
        self.assertEqual(client.calls, 10)

    def test_corrupt_entry_with_no_client_is_a_cachemiss_not_a_crash(self):
        r = self.run_all(FakeClient(), PAYLOADS[:1])[0]
        self.cache.path_for(r.key).write_text("garbage")
        with self.assertRaises(C.CacheMiss):
            self.run_all(None, PAYLOADS[:1])

    def test_missing_cache_dir_is_just_a_miss(self):
        self.assertIsNone(C.Cache(self.root / "does" / "not" / "exist").get("0" * 64))
        self.assertEqual(C.Cache(self.root / "does" / "not" / "exist").entries(), ([], 0))


class LedgerTests(Base):
    def test_one_line_per_lookup_with_hit_flag_tokens_latency_and_usd(self):
        client = FakeClient()
        self.run_all(client)
        self.run_all(client)
        rows, bad = self.ledger.rows()
        self.assertEqual((len(rows), bad), (10, 0))
        self.assertEqual([r["cache_hit"] for r in rows], [False] * 5 + [True] * 5)
        r = rows[0]
        self.assertEqual(sorted(r), ["cache_hit", "completion_tokens", "email_id", "error", "key", "latency_s", "model",
                                     "prompt_tokens", "prompt_version", "status", "usd"])
        self.assertEqual((r["status"], r["error"]), ("ok", None))
        self.assertEqual(r["usd"], round((r["prompt_tokens"] * 10 + r["completion_tokens"] * 20) / 1e6, 6))
        self.assertEqual(rows[5]["prompt_tokens"], rows[0]["prompt_tokens"])    # hits keep the original token counts

    def test_model_without_a_price_gets_null_usd_and_the_report_says_so(self):
        self.run_all(FakeClient(), model="unpriced-model")
        self.assertIsNone(self.ledger.rows()[0][0]["usd"])
        out = L.cost_from_ledger(self.ledger)
        self.assertIn("no price for model(s): unpriced-model", out)
        self.assertIn("unknown", out)
        self.assertIn("PLACEHOLDER", out)

    def test_unwritable_ledger_never_breaks_a_call(self):
        led = L.Ledger(self.root)                                       # a directory, so appending must fail
        r = K.get_or_call(PAYLOADS[0], FakeClient(), model=MODEL, prompt_version=PV, schema_version=SV, email_id="E000",
                          cache=self.cache, ledger=led, stats=self.stats)
        self.assertFalse(r.cache_hit)
        self.assertEqual(led.write_errors, 1)

    def test_unreadable_ledger_lines_are_skipped(self):
        self.run_all(FakeClient(), PAYLOADS[:2])
        with open(self.ledger.path, "a", encoding="utf-8") as f:
            f.write("not json\n[1,2]\n")
        rows, bad = self.ledger.rows()
        self.assertEqual((len(rows), bad), (2, 2))


class CostReportTests(Base):
    def test_cost_from_cache_entries_needs_no_ledger_and_no_client(self):
        client = FakeClient()
        results = self.run_all(client)
        self.ledger.path.unlink()                                       # prove the cache alone is enough
        out = L.cost_from_cache(C.Cache(self.cache.dir))                # a fresh object, as a separate process would
        total_pt = sum(r.usage["prompt_tokens"] for r in results)
        total_usd = sum(r.usd for r in results)
        self.assertIn("5 entries (0 unreadable", out)
        for p in PAYLOADS:
            self.assertIn(p["email"], out)
        total_line = [l for l in out.splitlines() if l.startswith("TOTAL")][0].split()
        self.assertEqual(total_line[1], "5")                            # model calls
        self.assertEqual(int(total_line[2]), total_pt)
        self.assertEqual(int(total_line[3]), 100)                       # 5 calls x 20 completion tokens
        self.assertEqual(total_line[4], f"${total_usd:,.4f}")
        self.assertIn("PLACEHOLDER", out)

    def test_cost_from_ledger_separates_real_calls_from_hits(self):
        client = FakeClient()
        self.run_all(client)
        self.run_all(client)
        out = L.cost_from_ledger(self.ledger)
        total = [l for l in out.splitlines() if l.startswith("TOTAL")][0].split()
        self.assertEqual(total[1:4], ["10", "5", "5"])                  # lookups, real calls, cache hits
        e0 = [l for l in out.splitlines() if l.startswith("E000")][0].split()
        self.assertEqual(e0[1:4], ["2", "1", "1"])
        spent, priced = total[-1], total[-2]
        self.assertNotEqual(spent, priced)                              # hits are not charged again

    def test_placeholder_banner_only_while_prices_are_placeholders(self):
        self.run_all(FakeClient(), PAYLOADS[:1])
        self.assertIn("PLACEHOLDER", L.cost_from_cache(self.cache))
        with mock.patch.object(config, "PRICES_ARE_PLACEHOLDER", False):
            self.assertNotIn("PLACEHOLDER", L.cost_from_cache(self.cache))
            self.assertNotIn("PLACEHOLDER", L.cost_from_ledger(self.ledger))

    def test_cost_report_ignores_corrupt_entries_and_handles_empty(self):
        self.run_all(FakeClient(), PAYLOADS[:2])
        (self.cache.dir / ("f" * 64 + ".json")).write_text("junk")
        self.assertIn("2 entries (1 unreadable", L.cost_from_cache(self.cache))
        self.assertIn("no cache entries", L.cost_from_cache(C.Cache(self.root / "empty")))
        self.assertIn("ledger is empty", L.cost_from_ledger(L.Ledger(self.root / "none.jsonl")))

    def test_cli_cost_command_prints_the_report(self):
        import io
        from contextlib import redirect_stdout
        from rfqparse.__main__ import main
        self.run_all(FakeClient())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["cost", "--cache-dir", str(self.cache.dir)])
        self.assertEqual(code, 0)
        self.assertIn("TOTAL", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["cost", "--ledger", str(self.ledger.path)])
        self.assertIn("COST FROM LEDGER", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
