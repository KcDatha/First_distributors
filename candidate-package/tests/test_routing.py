"""Tests for rfqparse.llm.routing: model routing and the validate-outside-the-cache escalation. Fake client only.

    python tests/test_routing.py -v
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from rfqparse import config  # noqa: E402
from rfqparse.llm import cache as C  # noqa: E402
from rfqparse.llm import call as K  # noqa: E402
from rfqparse.llm import errors as E  # noqa: E402
from rfqparse.llm import ledger as L  # noqa: E402
from rfqparse.llm import routing as R  # noqa: E402
from test_registry import Base as MailBase, make_mail, png, xlsx_bytes  # noqa: E402

MINI, FULL = "gpt-4.1-mini", "gpt-4.1"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["classification", "line_count"],
          "properties": {"classification": {"type": "string", "enum": ["rfq", "other"]}, "line_count": {"type": "integer"}}}
GOOD = {"classification": "rfq", "line_count": 2}
BAD = {"classification": "banana", "line_count": 2}
TEXT = [{"type": "text", "text": "2 ea elbow, 4 ea tee"}]
IMAGE = [{"type": "text", "text": "see picture"}, {"type": "image", "data": b"\x89PNGfake", "mime": "image/png", "detail": "high"}]


def reply(parsed, finish="stop", refusal=None):
    import json
    return {"parsed": parsed, "raw_text": json.dumps(parsed) if parsed is not None else "", "finish_reason": finish, "refusal": refusal}


class ScriptedClient:
    """Answers per model from a list of replies; counts calls and remembers which model was asked."""

    def __init__(self, **per_model):
        self.per_model, self.models_called = {k.replace("_", "-").replace("gpt-4-1", "gpt-4.1"): list(v) for k, v in per_model.items()}, []

    def complete(self, payload):
        m = payload["model"]
        self.models_called.append(m)
        return {"response": self.per_model[m].pop(0), "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


class RoutingFunctionTests(unittest.TestCase):
    def test_text_only_goes_to_the_text_model_and_any_image_goes_to_the_image_model(self):
        self.assertEqual(R.route_model(TEXT), MINI)
        self.assertEqual(R.route_model([]), MINI)
        self.assertEqual(R.route_model(IMAGE), FULL)
        self.assertEqual(R.route_model(TEXT + IMAGE[1:] + IMAGE[1:]), FULL)

    def test_it_is_config_driven(self):
        with mock.patch.object(config, "ROUTING", {"text_model": "a", "image_model": "b", "escalate_text_model_to": "c"}):
            self.assertEqual((R.route_model(TEXT), R.route_model(IMAGE)), ("a", "b"))
            self.assertEqual((R.escalation_model("a"), R.escalation_model("b")), ("c", None))

    def test_only_the_text_model_escalates_and_never_to_itself(self):
        self.assertEqual(R.escalation_model(MINI), FULL)
        self.assertIsNone(R.escalation_model(FULL))
        with mock.patch.object(config, "ROUTING", {**config.ROUTING, "escalate_text_model_to": MINI}):
            self.assertIsNone(R.escalation_model(MINI))

    def test_prices_and_settings_are_in_config(self):
        self.assertEqual(config.MODEL_PRICES[MINI], {"input_per_1m": 0.40, "output_per_1m": 1.60})
        self.assertEqual(config.MODEL_PRICES[FULL], {"input_per_1m": 2.00, "output_per_1m": 8.00})
        self.assertNotIn("placeholder-model", config.MODEL_PRICES)
        self.assertFalse(config.PRICES_ARE_PLACEHOLDER)
        self.assertEqual(config.MAX_USD_PER_RUN, 2.00)
        for m in (MINI, FULL):
            self.assertEqual(config.MODEL_SETTINGS[m]["temperature"], 0)
        self.assertEqual(R.config.ROUTING["escalate_text_model_to"], FULL)


class RoutingFromRenderedMailTests(MailBase):
    """What actually reaches the payload decides the route, not what was attached."""

    def parts_for(self, pkg):
        parts = [{"type": "text", "text": pkg.current_text}]
        for a in pkg.attachments:
            if a.text:
                parts.append({"type": "text", "text": a.text})
            for it in a.images:
                if it.result.ok:                      # only images that would really be sent
                    parts.append({"type": "image", "data": it.result.data, "mime": it.result.mime})
        return parts

    def test_spreadsheet_rendered_to_text_is_text(self):
        pkg = self.render(make_mail([(xlsx_bytes("widget"), "application", "octet-stream", "list.xlsx", {})]))
        self.assertEqual(len(pkg.attachments), 1)                     # an attachment exists...
        self.assertEqual(R.route_model(self.parts_for(pkg)), MINI)    # ...but no image goes to the model

    def test_skipped_tracking_pixel_does_not_count_as_an_image(self):
        b = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(b, "GIF")
        pkg = self.render(make_mail([(b.getvalue(), "image", "gif", "pixel.gif", {})]))
        self.assertEqual(pkg.attachments[0].status, "skipped")
        self.assertEqual(R.route_model(self.parts_for(pkg)), MINI)

    def test_any_real_image_counts_including_a_logo(self):
        pkg = self.render(make_mail([(png(300, 60, "blue"), "image", "png", "logo.png", {"disposition": "inline", "cid": "<l@x>"})]))
        self.assertEqual(R.route_model(self.parts_for(pkg)), FULL)


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cache, self.ledger, self.stats = C.Cache(root / "cache"), L.Ledger(root / "ledger.jsonl"), K.CallStats()

    def run_routed(self, client, user=TEXT, email_id="E001", **kw):
        return R.call_routed(system="classify", user=user, schema=SCHEMA, client=client, email_id=email_id,
                             prompt_version="v1", schema_version="s1", cache=self.cache, ledger=self.ledger,
                             stats=self.stats, **kw)

    def test_good_answer_from_the_text_model_does_not_escalate(self):
        cl = ScriptedClient(gpt_4_1_mini=[reply(GOOD)])
        r = self.run_routed(cl)
        self.assertEqual((r.model, r.escalated, r.parsed), (MINI, False, GOOD))
        self.assertEqual(cl.models_called, [MINI])

    def test_failed_validation_retries_once_on_the_big_model_and_BOTH_attempts_are_cached(self):
        cl = ScriptedClient(gpt_4_1_mini=[reply(BAD)], gpt_4_1=[reply(GOOD)])
        r = self.run_routed(cl)
        self.assertEqual(cl.models_called, [MINI, FULL])
        self.assertEqual((r.model, r.escalated, r.parsed), (FULL, True, GOOD))
        self.assertEqual([m for m, _ in r.results], [MINI, FULL])
        entries, _ = self.cache.entries()
        self.assertEqual(sorted(e.model for e in entries), [FULL, MINI])           # the bad answer is cached too
        bad_entry = next(e for e in entries if e.model == MINI)
        self.assertEqual(bad_entry.response["parsed"], BAD)                        # stored as received, not "fixed"
        self.assertEqual((self.stats.real_calls, self.stats.cache_hits), (2, 0))

    def test_replay_with_no_key_takes_the_same_path_from_the_cache(self):
        cl = ScriptedClient(gpt_4_1_mini=[reply(BAD)], gpt_4_1=[reply(GOOD)])
        first = self.run_routed(cl)
        before = self.stats.real_calls
        replay = self.run_routed(None)                                             # no client at all
        self.assertEqual(self.stats.real_calls, before)                            # zero new calls
        self.assertEqual((replay.model, replay.escalated, replay.parsed), (first.model, first.escalated, first.parsed))
        self.assertEqual([m for m, _ in replay.results], [MINI, FULL])             # same two lookups, in the same order
        self.assertTrue(all(res.cache_hit for _, res in replay.results))
        self.assertEqual(self.stats.cache_hits, 2)

    def test_both_attempts_failing_raises_the_typed_error_and_replays_the_same_error(self):
        cl = ScriptedClient(gpt_4_1_mini=[reply(BAD)], gpt_4_1=[reply({"classification": "rfq"})])
        with self.assertRaises(E.SchemaMismatch) as first:
            self.run_routed(cl)
        self.assertEqual(cl.models_called, [MINI, FULL])                           # exactly one retry, no more
        self.assertEqual([m for m, _ in first.exception.attempts], [MINI, FULL])
        with self.assertRaises(E.SchemaMismatch) as again:
            self.run_routed(None)                                                  # replay: same verdict, not a CacheMiss
        self.assertEqual(str(again.exception), str(first.exception))
        self.assertEqual(self.stats.real_calls, 2)

    def test_image_emails_are_not_retried_because_they_are_already_on_the_big_model(self):
        cl = ScriptedClient(gpt_4_1=[reply(BAD)])
        with self.assertRaises(E.SchemaMismatch):
            self.run_routed(cl, user=IMAGE)
        self.assertEqual(cl.models_called, [FULL])

    def test_refusal_and_truncation_from_the_text_model_also_escalate(self):
        for bad in (reply(None, refusal="can't"), reply({"classification": "rf"}, finish="length"), reply(None)):
            with self.subTest(str(bad)[:40]):
                self.setUp()
                cl = ScriptedClient(gpt_4_1_mini=[bad], gpt_4_1=[reply(GOOD)])
                r = self.run_routed(cl)
                self.assertEqual((cl.models_called, r.escalated), ([MINI, FULL], True))

    def test_semantic_validator_outside_the_cache_triggers_escalation(self):
        def validator(p):
            return ["line_count is 0 but the email has items"] if p["line_count"] == 0 else []
        cl = ScriptedClient(gpt_4_1_mini=[reply({"classification": "rfq", "line_count": 0})], gpt_4_1=[reply(GOOD)])
        r = self.run_routed(cl, validate=validator)
        self.assertEqual((cl.models_called, r.parsed), ([MINI, FULL], GOOD))
        # the cache stored the schema-valid-but-rejected answer untouched; the verdict is not in the cache layer
        e = next(x for x in self.cache.entries()[0] if x.model == MINI)
        self.assertEqual(e.response["parsed"]["line_count"], 0)

    def test_get_or_call_alone_never_judges_a_reply(self):
        payload = {"model": MINI, "system": "s", "user": TEXT, "schema": SCHEMA, "schema_name": "x"}
        res = K.get_or_call(payload, ScriptedClient(gpt_4_1_mini=[reply(BAD)]), model=MINI, prompt_version="v1",
                            schema_version="s1", email_id="E001", cache=self.cache, ledger=self.ledger, stats=self.stats)
        self.assertEqual(res.response["parsed"], BAD)                              # returned and cached without a verdict
        self.assertIsNotNone(self.cache.get(res.key))

    def test_api_failure_on_the_first_try_does_not_escalate(self):
        class Down:
            reports_attempts = False

            def __init__(self):
                self.calls = []

            def complete(self, payload):
                self.calls.append(payload["model"])
                raise RuntimeError("provider down")
        d = Down()
        with self.assertRaises(E.LLMCallError):
            self.run_routed(d)
        self.assertEqual(d.calls, [MINI])                                          # escalation is for bad answers, not outages

    def test_budget_exceeded_on_the_second_attempt_propagates_as_a_miss(self):
        class Budget:
            def __init__(self):
                self.n = 0

            def complete(self, payload):
                self.n += 1
                if self.n == 2:
                    raise E.BudgetExceeded(2.0, 2.0, payload["model"])
                return {"response": reply(BAD), "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        with self.assertRaises(C.CacheMiss):
            self.run_routed(Budget())

    def test_ledger_has_one_ok_line_per_attempt_with_each_models_price(self):
        cl = ScriptedClient(gpt_4_1_mini=[reply(BAD)], gpt_4_1=[reply(GOOD)])
        self.run_routed(cl)
        rows, _ = self.ledger.rows()
        self.assertEqual([(r["model"], r["status"]) for r in rows], [(MINI, "ok"), (FULL, "ok")])
        self.assertEqual(rows[0]["usd"], round((1000 * 0.40 + 100 * 1.60) / 1e6, 6))
        self.assertEqual(rows[1]["usd"], round((1000 * 2.00 + 100 * 8.00) / 1e6, 6))


if __name__ == "__main__":
    unittest.main()
