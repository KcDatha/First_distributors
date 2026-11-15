"""The smoke script's flow, run against a fake transport (no real calls): two calls, then zero.

    python tests/test_smoke_flow.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "eval"))
sys.path.insert(0, str(HERE))
import smoke_openai as SM  # noqa: E402
from rfqparse import config  # noqa: E402
from rfqparse.llm import cache as C  # noqa: E402
from rfqparse.llm import call as K  # noqa: E402
from rfqparse.llm import ledger as L  # noqa: E402
from test_llm_client import KEY, completion, make  # noqa: E402


class SmokeFlow(unittest.TestCase):
    def test_one_call_per_model_then_zero(self):
        def reply(request):
            body = json.loads(request.content)
            has_img = any(p["type"] == "image_url" for p in body["messages"][1]["content"])
            content = ('{"classification":"rfq","line_count":6,"text_read":"6 items"}' if has_img
                       else '{"classification":"rfq","line_count":2}')
            return completion(content=content)

        emails = HERE.parent / "data" / "dev" / "emails"
        with tempfile.TemporaryDirectory() as t:
            cache, ledger, stats = C.Cache(Path(t) / "c"), L.Ledger(Path(t) / "l.jsonl"), K.CallStats()
            requests = []
            cl, _ = make(lambda r: (requests.append(r), reply(r))[1])
            first = SM.run_smoke(cl, cache, ledger, stats, emails)
            self.assertEqual((len(requests), stats.real_calls, stats.cache_hits), (2, 2, 0))
            self.assertEqual([json.loads(r.content)["model"] for r in requests], ["gpt-4.1-mini", "gpt-4.1"])
            self.assertEqual([r["model"] for r in first], ["gpt-4.1-mini", "gpt-4.1"])
            self.assertEqual([r["parsed"]["line_count"] for r in first], [2, 6])
            self.assertFalse(any(r["escalated"] for r in first))
            text_body = json.loads(requests[0].content)["messages"][1]["content"]
            self.assertFalse(any(p["type"] == "image_url" for p in text_body))          # the text call carries no image
            self.assertTrue(any(p["type"] == "image_url" for p in json.loads(requests[1].content)["messages"][1]["content"]))
            second = SM.run_smoke(cl, cache, ledger, stats, emails)
            self.assertEqual((len(requests), stats.real_calls, stats.cache_hits), (2, 2, 2))   # zero new calls
            self.assertTrue(all(r["cache_hit"] for r in second))
            out = L.cost_from_ledger(ledger)
            self.assertIn("SMOKE-image", out)
            self.assertNotIn("PLACEHOLDER", out)
            self.assertNotIn(KEY, (Path(t) / "l.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
