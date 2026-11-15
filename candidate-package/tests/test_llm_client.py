"""Tests for rfqparse.llm.client / errors / schema using a fake HTTP transport. No real calls, no real key.

    python tests/test_llm_client.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rfqparse import config  # noqa: E402
from rfqparse.llm import cache as C  # noqa: E402
from rfqparse.llm import call as K  # noqa: E402
from rfqparse.llm import client as CL  # noqa: E402
from rfqparse.llm import errors as E  # noqa: E402
from rfqparse.llm import ledger as L  # noqa: E402
from rfqparse.llm import schema as S  # noqa: E402

KEY = "sk-test-FAKEKEY-1234567890abcdef"
MODEL = "fake-model"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["classification", "line_count"],
          "properties": {"classification": {"type": "string", "enum": ["rfq", "other"]}, "line_count": {"type": "integer"}}}
PRICES = {MODEL: {"input_per_1m": 10.0, "output_per_1m": 20.0}}
USER = [{"type": "text", "text": "Need 2 ea 2in elbow and 4 ea 2in tee"}]


def completion(content='{"classification":"rfq","line_count":2}', finish="stop", refusal=None, usage=(100, 20)):
    msg = {"role": "assistant", "content": content}
    if refusal is not None:
        msg["refusal"], msg["content"] = refusal, None
    return httpx2.Response(200, json={"id": "c1", "object": "chat.completion", "created": 1, "model": MODEL,
                                      "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
                                      "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}})


def error(status, code=None, headers=None, message="bad"):
    return httpx2.Response(status, headers=headers or {}, json={"error": {"message": message, "type": "x", "code": code}})


class Script:
    """A transport that plays back a list of responses (or exceptions) and records the requests."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def __call__(self, request):
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step


def make(script, **kw):
    sleeps = []
    cl = CL.make_client(api_key=KEY, http_client=httpx2.Client(transport=httpx2.MockTransport(script)),
                        sleep=sleeps.append, rng=lambda: 0.5, **kw)
    return cl, sleeps


class Base(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(config, "MODEL_PRICES", PRICES),
                  mock.patch.object(config, "MODEL_SETTINGS", {MODEL: {"temperature": 0, "max_completion_tokens": 500}}),
                  mock.patch.object(config, "LLM_MAX_ATTEMPTS", 3),
                  mock.patch.object(config, "LLM_BACKOFF_BASE_S", 1.0),
                  mock.patch.object(config, "LLM_BACKOFF_MAX_S", 20.0)):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def payload(self, **kw):
        return CL.build_payload(model=MODEL, system="classify", user=kw.get("user", USER), schema=SCHEMA)

    def go(self, script, **kw):
        cl, sleeps = make(script, **kw)
        return cl, sleeps, cl.complete(self.payload())


class RequestShapeTests(Base):
    def test_request_is_strict_structured_temp_zero_no_sdk_retries_and_key_only_in_the_header(self):
        script = Script(completion())
        cl, _, out = self.go(script)
        self.assertEqual(cl._sdk.max_retries, 0)
        body = json.loads(script.requests[0].content)
        rf = body["response_format"]
        self.assertEqual((rf["type"], rf["json_schema"]["strict"], rf["json_schema"]["schema"]), ("json_schema", True, SCHEMA))
        self.assertEqual((body["temperature"], body["max_completion_tokens"], body["model"]), (0, 500, MODEL))
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertIn(KEY, script.requests[0].headers["authorization"])
        self.assertNotIn(KEY, script.requests[0].content.decode())
        self.assertEqual(out["response"]["parsed"], {"classification": "rfq", "line_count": 2})
        self.assertEqual(out["usage"], {"prompt_tokens": 100, "completion_tokens": 20})

    def test_temperature_omitted_for_models_that_reject_it(self):
        with mock.patch.object(config, "MODEL_SETTINGS", {MODEL: {"temperature": None, "max_completion_tokens": 500}}):
            script = Script(completion())
            self.go(script)
        self.assertNotIn("temperature", json.loads(script.requests[0].content))

    def test_images_are_sent_as_data_urls_with_detail(self):
        user = USER + [{"type": "image", "data": b"\x89PNGfake", "mime": "image/png", "detail": "high"}]
        script = Script(completion())
        cl, _ = make(script)
        cl.complete(self.payload(user=user))
        part = json.loads(script.requests[0].content)["messages"][1]["content"][1]
        self.assertTrue(part["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(part["image_url"]["detail"], "high")

    def test_non_strict_schema_is_caught_before_any_request(self):
        script = Script(completion())
        cl, _ = make(script)
        bad = CL.build_payload(model=MODEL, system="s", user=USER, schema={"type": "object", "properties": {"a": {"type": "string"}}})
        with self.assertRaises(ValueError):
            cl.complete(bad)
        self.assertEqual(script.requests, [])

    def test_unpriced_model_is_refused_before_any_request(self):
        script = Script(completion())
        cl, _ = make(script)
        with self.assertRaises(E.ModelNotPriced):
            cl.complete(CL.build_payload(model="mystery-model", system="s", user=USER, schema=SCHEMA))
        self.assertEqual(script.requests, [])


class RetryTests(Base):
    def test_429_then_success_retries_with_backoff_and_jitter(self):
        script = Script(error(429, "rate_limit_exceeded"), completion())
        attempts = []
        cl, sleeps = make(script)
        out = cl.complete(self.payload(), on_attempt=attempts.append)
        self.assertEqual(len(script.requests), 2)
        self.assertEqual(out["response"]["parsed"]["line_count"], 2)
        self.assertEqual(len(attempts), 1)                              # the failed try was reported
        self.assertEqual(attempts[0]["error"].kind, "rate_limit")
        self.assertTrue(attempts[0]["will_retry"])
        self.assertEqual(sleeps, [0.75])                                # base 1s: cap/2 + 0.5 * cap/2

    def test_backoff_grows_and_is_capped(self):
        with mock.patch.object(config, "LLM_MAX_ATTEMPTS", 5), mock.patch.object(config, "LLM_BACKOFF_MAX_S", 3.0):
            script = Script(error(503), error(503), error(503), error(503), completion())
            cl, sleeps = make(script)
            cl.complete(self.payload())
        self.assertEqual(sleeps, [0.75, 1.5, 2.25, 2.25])               # 1, 2, then capped at 3 (x0.75 with this jitter)

    def test_jitter_range(self):
        for r, expect in ((0.0, 0.5), (1.0, 1.0)):
            script = Script(error(500), completion())
            sleeps = []
            cl = CL.make_client(api_key=KEY, http_client=httpx2.Client(transport=httpx2.MockTransport(script)),
                                sleep=sleeps.append, rng=lambda r=r: r)
            cl.complete(self.payload())
            self.assertEqual(sleeps, [expect])

    def test_retry_after_header_is_honoured(self):
        script = Script(error(429, headers={"retry-after": "7"}), completion())
        _, sleeps, _ = self.go(script)
        self.assertEqual(sleeps, [7.0])

    def test_5xx_retries_then_gives_up_with_a_typed_error_and_every_attempt_reported(self):
        script = Script(error(502))
        attempts = []
        cl, sleeps = make(script)
        with self.assertRaises(E.ApiFailure) as cm:
            cl.complete(self.payload(), on_attempt=attempts.append)
        self.assertEqual(len(script.requests), 3)                       # config.LLM_MAX_ATTEMPTS
        self.assertEqual((cm.exception.kind, cm.exception.status_code, cm.exception.attempts), ("server", 502, 3))
        self.assertEqual(len(attempts), 3)
        self.assertEqual([a["will_retry"] for a in attempts], [True, True, False])
        self.assertEqual(len(sleeps), 2)

    def test_timeouts_and_connection_errors_are_retried(self):
        for exc, kind in ((httpx2.ReadTimeout("slow"), "timeout"), (httpx2.ConnectError("refused"), "connection")):
            with self.subTest(kind):
                script = Script(exc, completion())
                attempts = []
                cl, _ = make(script)
                cl.complete(self.payload(), on_attempt=attempts.append)
                self.assertEqual(len(script.requests), 2)
                self.assertEqual(attempts[0]["error"].kind, kind)

    def test_400_and_401_are_not_retried(self):
        for status, kind in ((400, "client_error"), (401, "auth"), (404, "client_error"), (422, "client_error")):
            with self.subTest(status):
                script = Script(error(status))
                cl, sleeps = make(script)
                with self.assertRaises(E.ApiFailure) as cm:
                    cl.complete(self.payload())
                self.assertEqual((len(script.requests), sleeps), (1, []))
                self.assertEqual((cm.exception.kind, cm.exception.status_code, cm.exception.attempts), (kind, status, 1))

    def test_out_of_quota_429_is_not_retried(self):
        script = Script(error(429, "insufficient_quota"))
        cl, _ = make(script)
        with self.assertRaises(E.ApiFailure) as cm:
            cl.complete(self.payload())
        self.assertEqual((len(script.requests), cm.exception.kind), (1, "quota"))

    def test_errors_never_contain_the_key(self):
        leaky = f"Incorrect API key provided: {KEY}. Bearer {KEY}"
        for status in (401, 400, 500):
            with self.subTest(status):
                cl, _ = make(Script(error(status, message=leaky)))
                with self.assertRaises(E.ApiFailure) as cm:
                    cl.complete(self.payload())
                self.assertNotIn(KEY, str(cm.exception))
                self.assertNotIn("FAKEKEY", repr(cm.exception))
        self.assertNotIn(KEY, repr(cl))


class ReplyHandlingTests(Base):
    """A reply that arrived is returned as is (so it can be cached); check_reply() judges it outside the cache layer."""

    def run_reply(self, response):
        script = Script(response)
        cl, _ = make(script)
        attempts = []
        out = cl.complete(self.payload(), on_attempt=attempts.append)          # does NOT raise for a bad reply
        parsed, err = CL.check_reply(out["response"], SCHEMA)
        return parsed, err, script, attempts, out

    def test_good_reply(self):
        parsed, err, *_ = self.run_reply(completion())
        self.assertEqual((parsed, err), ({"classification": "rfq", "line_count": 2}, None))

    def test_refusal(self):
        parsed, exc, script, attempts, out = self.run_reply(completion(refusal="I can't help with that"))
        self.assertIsInstance(exc, E.ModelRefusal)
        self.assertEqual(exc.refusal, "I can't help with that")
        self.assertEqual((len(script.requests), attempts), (1, []))             # one request, not retried, it was billed
        self.assertEqual(out["usage"]["prompt_tokens"], 100)

    def test_content_filter_counts_as_a_refusal(self):
        self.assertIsInstance(self.run_reply(completion(content="", finish="content_filter"))[1], E.ModelRefusal)

    def test_output_cut_off_by_the_length_limit(self):
        parsed, exc, script, attempts, out = self.run_reply(completion(content='{"classification":"rf', finish="length"))
        self.assertIsInstance(exc, E.OutputTruncated)
        self.assertEqual(len(script.requests), 1)
        self.assertEqual(out["response"]["finish_reason"], "length")

    def test_reply_that_does_not_match_the_schema(self):
        cases = {
            "wrong type": '{"classification":"rfq","line_count":"two"}',
            "missing field": '{"classification":"rfq"}',
            "extra field": '{"classification":"rfq","line_count":2,"note":"hi"}',
            "bad enum": '{"classification":"banana","line_count":2}',
            "not json": "Sure! Here is the JSON: {",
            "empty": "",
            "not an object": "[1,2]",
        }
        for name, content in cases.items():
            with self.subTest(name):
                parsed, exc, script, *_ = self.run_reply(completion(content=content))
                self.assertIsNone(parsed)
                self.assertIsInstance(exc, E.SchemaMismatch)
                self.assertEqual(len(script.requests), 1)

    def test_extra_validator_catches_what_the_schema_cannot_express(self):
        out = Script(completion(content='{"classification":"rfq","line_count":-3}'))
        cl, _ = make(out)
        resp = cl.complete(self.payload())["response"]
        parsed, err = CL.check_reply(resp, SCHEMA, lambda p: ["line_count must not be negative"] if p["line_count"] < 0 else [])
        self.assertIsNone(parsed)
        self.assertIn("line_count must not be negative", str(err))
        self.assertIsNone(CL.check_reply(resp, SCHEMA)[1])                       # the schema alone accepts it

    def test_strict_call_model_raises_typed_errors_and_reports_the_attempt(self):
        cases = ((completion(refusal="no"), E.ModelRefusal), (completion(finish="length"), E.OutputTruncated),
                 (completion(content="nope"), E.SchemaMismatch))
        for resp, exc in cases:
            with self.subTest(exc.__name__):
                cl, _ = make(Script(resp))
                attempts = []
                with self.assertRaises(exc):
                    CL.call_model(cl._sdk, system="s", user=USER, schema=SCHEMA, model=MODEL, on_attempt=attempts.append)
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]["prompt_tokens"], 100)

    def test_nothing_escapes_that_is_not_a_typed_error(self):
        script = Script(httpx2.Response(200, json={"id": "c", "object": "chat.completion", "created": 1, "model": MODEL, "choices": []}))
        cl, _ = make(script)
        with self.assertRaises(E.LLMCallError):
            cl.complete(self.payload())


class KeyAndBudgetTests(Base):
    def test_missing_key_is_a_typed_error(self):
        for env in ({}, {"OPENAI_API_KEY": ""}, {"OPENAI_API_KEY": "   "}):
            with self.subTest(env), mock.patch.dict(os.environ, env, clear=True), self.assertRaises(E.MissingKey):
                CL.make_client()

    def test_key_is_read_from_the_environment(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": KEY}, clear=True):
            cl = CL.make_client()
        self.assertEqual(cl._sdk.api_key, KEY)
        self.assertNotIn("FAKEKEY", repr(cl))

    def test_spend_guard_stops_real_calls_without_touching_the_network(self):
        # each call costs (100*10 + 20*20)/1e6 = $0.0014
        script = Script(completion())
        cl, _ = make(script, guard=CL.SpendGuard(limit_usd=0.0020))
        cl.complete(self.payload())
        cl.complete(self.payload())                                             # $0.0028 spent: over the limit
        n = len(script.requests)
        with self.assertRaises(E.BudgetExceeded) as cm:
            cl.complete(self.payload())
        self.assertEqual(len(script.requests), n)
        self.assertIsInstance(cm.exception, C.CacheMiss)                         # the pipeline treats it as a miss
        self.assertAlmostEqual(cm.exception.spent, 0.0028, places=6)

    def test_budget_is_counted_from_billed_replies_even_when_unusable(self):
        cl, _ = make(Script(completion(content="nope")), guard=CL.SpendGuard(limit_usd=0.0010))
        cl.complete(self.payload())                                             # unusable reply, but billed
        with self.assertRaises(E.BudgetExceeded):
            cl.complete(self.payload())

    def test_default_limit_comes_from_config(self):
        with mock.patch.object(config, "MAX_USD_PER_RUN", 1.23):
            self.assertEqual(CL.SpendGuard().limit, 1.23)


class LedgerIntegrationTests(Base):
    def setUp(self):
        super().setUp()
        self.cache, self.ledger, self.stats = C.Cache(self.root / "cache"), L.Ledger(self.root / "ledger.jsonl"), K.CallStats()

    def lookup(self, client, email_id="E001"):
        return K.get_or_call(self.payload(), client, model=MODEL, prompt_version="v1", schema_version="s1",
                             email_id=email_id, cache=self.cache, ledger=self.ledger, stats=self.stats)

    def test_every_attempt_gets_a_ledger_line_and_failed_ones_have_usd_null(self):
        cl, _ = make(Script(error(429), error(503), completion()))
        r = self.lookup(cl)
        rows, _ = self.ledger.rows()
        self.assertEqual([x["status"] for x in rows], ["error", "error", "ok"])
        self.assertEqual([x["usd"] for x in rows[:2]], [None, None])
        self.assertEqual(rows[0]["error"], "ApiFailure status=429")
        self.assertEqual(rows[2]["usd"], r.usd)
        self.assertEqual((self.stats.real_calls, self.stats.failed_attempts), (1, 2))
        self.assertNotIn(KEY, (self.root / "ledger.jsonl").read_text())

    def test_second_lookup_is_a_hit_with_zero_requests(self):
        script = Script(completion())
        cl, _ = make(script)
        self.lookup(cl)
        self.lookup(cl)
        self.assertEqual(len(script.requests), 1)
        self.assertEqual((self.stats.real_calls, self.stats.cache_hits), (1, 1))

    def test_exhausted_retries_raise_typed_error_and_leave_error_lines_and_no_cache_entry(self):
        cl, _ = make(Script(error(500)))
        with self.assertRaises(E.ApiFailure):
            self.lookup(cl)
        rows, _ = self.ledger.rows()
        self.assertEqual([x["status"] for x in rows], ["error"] * 3)
        self.assertEqual(self.cache.entries()[0], [])

    def test_non_retryable_failure_is_one_error_line(self):
        cl, _ = make(Script(error(400)))
        with self.assertRaises(E.ApiFailure):
            self.lookup(cl)
        self.assertEqual([x["status"] for x in self.ledger.rows()[0]], ["error"])

    def test_refusal_and_truncation_are_billed_cached_and_logged_as_ok_lines(self):
        for resp, exc in ((completion(refusal="no"), E.ModelRefusal), (completion(finish="length"), E.OutputTruncated)):
            with self.subTest(exc.__name__):
                self.ledger.path.unlink(missing_ok=True)
                cl, _ = make(Script(resp))
                r = self.lookup(cl)                                              # get_or_call itself does not judge the reply
                row = self.ledger.rows()[0][0]
                self.assertEqual((row["status"], row["prompt_tokens"], row["usd"]), ("ok", 100, r.usd))
                self.assertIsNotNone(self.cache.get(r.key))                      # cached, so a replay needs no key
                self.assertIsInstance(CL.check_reply(r.response, SCHEMA)[1], exc)
                self.cache.path_for(r.key).unlink()

    def test_budget_exceeded_passes_through_get_or_call_as_a_miss_and_cache_hits_still_work(self):
        cl, _ = make(Script(completion()), guard=CL.SpendGuard(limit_usd=0.001))
        self.lookup(cl, "E001")                                                   # spends $0.0014, over the limit
        other = CL.build_payload(model=MODEL, system="classify", user=[{"type": "text", "text": "different"}], schema=SCHEMA)
        with self.assertRaises(C.CacheMiss) as cm:
            K.get_or_call(other, cl, model=MODEL, prompt_version="v1", schema_version="s1", email_id="E002",
                          cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertIsInstance(cm.exception, E.BudgetExceeded)
        self.assertTrue(self.lookup(cl, "E001").cache_hit)                         # replaying the cache needs no budget

    def test_client_that_does_not_report_attempts_still_leaves_an_error_line(self):
        class Boom:
            def complete(self, payload):
                raise RuntimeError(f"upstream said {KEY}")
        with self.assertRaises(E.LLMCallError) as cm:
            self.lookup(Boom())
        self.assertNotIn(KEY, str(cm.exception))
        row = self.ledger.rows()[0][0]
        self.assertEqual((row["status"], row["usd"]), ("error", None))
        self.assertNotIn(KEY, (self.root / "ledger.jsonl").read_text())

    def test_key_never_reaches_the_cache_entry(self):
        cl, _ = make(Script(completion()))
        r = self.lookup(cl)
        self.assertNotIn(KEY, self.cache.path_for(r.key).read_text())
        self.assertEqual(self.cache.get(r.key).response["parsed"], {"classification": "rfq", "line_count": 2})

    def test_cost_report_shows_failed_tries(self):
        cl, _ = make(Script(error(429), completion()))
        self.lookup(cl)
        out = L.cost_from_ledger(self.ledger)
        row = [l for l in out.splitlines() if l.startswith("TOTAL")][0].split()
        self.assertEqual(row[1:5], ["1", "1", "0", "100"])                        # lookups, real, hits, prompt tokens
        self.assertIn("failed tries", out)
        self.assertEqual(row[6], "1")                                              # one failed try


class SchemaTests(unittest.TestCase):
    def test_validate_supported_keywords(self):
        sch = {"type": "object", "additionalProperties": False, "required": ["a", "b"], "properties": {
            "a": {"type": ["string", "null"]}, "b": {"type": "array", "items": {"type": "integer"}},
            "c": {"anyOf": [{"type": "string"}, {"type": "number"}]}}}
        self.assertEqual(S.validate({"a": None, "b": [1, 2]}, sch), [])
        self.assertEqual(S.validate({"a": "x", "b": [], "c": 1.5}, sch), [])
        self.assertTrue(S.validate({"a": "x", "b": [1, "2"]}, sch))
        self.assertTrue(S.validate({"a": "x", "b": [], "c": True}, sch))
        self.assertTrue(S.validate({"a": 1, "b": []}, sch))
        self.assertFalse(S.validate({"a": "x", "b": [True]}, {"type": "object", "properties": {"b": {"type": "array", "items": {"type": "boolean"}}}}))
        self.assertTrue(S.validate({"n": True}, {"type": "object", "properties": {"n": {"type": "integer"}}}))   # bool is not an int

    def test_refs(self):
        sch = {"type": "object", "additionalProperties": False, "required": ["x"], "properties": {"x": {"$ref": "#/$defs/n"}},
               "$defs": {"n": {"type": "integer"}}}
        self.assertEqual(S.validate({"x": 1}, sch), [])
        self.assertTrue(S.validate({"x": "1"}, sch))
        S.check_strict(sch)

    def test_check_strict(self):
        S.check_strict(SCHEMA)
        for bad in ({"type": "object", "properties": {"a": {"type": "string"}}},
                    {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}, "required": []},
                    {"type": "object", "additionalProperties": False, "required": ["l"],
                     "properties": {"l": {"type": "array", "items": {"type": "object", "properties": {"q": {"type": "string"}}}}}}):
            with self.assertRaises(ValueError):
                S.check_strict(bad)


if __name__ == "__main__":
    unittest.main()
