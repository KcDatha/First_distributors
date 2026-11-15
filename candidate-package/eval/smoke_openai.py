#!/usr/bin/env python3
"""Two cheap real calls, one per model, then the same two again to show 0 new calls.

    python eval/smoke_openai.py          (needs OPENAI_API_KEY in the environment)

The made-up text request is routed to the text model and the E010 picture to the image model (config.ROUTING),
through the same call_routed() path the pipeline will use. Uses its own cache and ledger under build/ so the
real cache/ and cost_ledger.jsonl stay clean.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rfqparse import config  # noqa: E402
from rfqparse.ingest import ingest_eml  # noqa: E402
from rfqparse.llm import client as CL  # noqa: E402
from rfqparse.llm.cache import Cache  # noqa: E402
from rfqparse.llm.call import CallStats  # noqa: E402
from rfqparse.llm.routing import call_routed  # noqa: E402
from rfqparse.llm.errors import LLMCallError, MissingKey  # noqa: E402
from rfqparse.llm.ledger import Ledger, cost_from_ledger  # noqa: E402
from rfqparse.render.image import prepare_image  # noqa: E402

CLASSES = ["rfq", "rfq_revision", "purchase_order", "vendor_quote", "status_inquiry", "auto_reply", "other"]
TEXT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["classification", "line_count"],
               "properties": {"classification": {"type": "string", "enum": CLASSES}, "line_count": {"type": "integer"}}}
IMAGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["classification", "line_count", "text_read"],
                "properties": {**TEXT_SCHEMA["properties"], "text_read": {"type": "string"}}}
MADE_UP = "Please price the following:\n2 ea 3in 150# slip-on flange\n40 ft 2in SCH 40 pipe"


def build_calls(dev_emails: Path) -> list[tuple[str, str, dict]]:
    """[(email id, label, call args)]: a text prompt, and the E010 spreadsheet screenshot."""
    text = dict(
        schema=TEXT_SCHEMA, schema_name="smoke_text",
        system="You read customer emails for an industrial supply company. Classify the email and count the line items requested.",
        user=[{"type": "text", "text": MADE_UP}])
    mail = ingest_eml(dev_emails / "E010.eml")
    att = next(a for a in mail.attachments if a.filename == "image002.png")
    img = prepare_image(att.data)
    image = dict(
        schema=IMAGE_SCHEMA, schema_name="smoke_image",
        system="You read pictures attached to customer emails for an industrial supply company. Read the text in the picture, "
               "classify it like an email, and count the item rows.",
        user=[{"type": "text", "text": "What does this picture say?"},
              {"type": "image", "data": img.data, "mime": img.mime, "detail": config.LLM_IMAGE_DETAIL}])
    return [("SMOKE-text", "text prompt", text), ("SMOKE-image", "E010 image002.png", image)]


def run_smoke(client, cache: Cache, ledger: Ledger, stats: CallStats, dev_emails: Path) -> list[dict]:
    out = []
    for eid, label, args in build_calls(dev_emails):
        r = call_routed(client=client, email_id=eid, prompt_version="smoke-1", schema_version="smoke-1",
                        cache=cache, ledger=ledger, stats=stats, **args)
        out.append({"email_id": eid, "label": label, "model": r.model, "escalated": r.escalated, "parsed": r.parsed,
                    "usage": r.results[-1][1].usage, "cache_hit": all(x.cache_hit for _, x in r.results),
                    "latency_s": round(sum(x.latency_s for _, x in r.results), 3), "usd": r.usd})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dev-emails", type=Path, default=ROOT / "data" / "dev" / "emails")
    a = ap.parse_args(argv)
    cache, ledger, stats = Cache(ROOT / "build" / "smoke_cache"), Ledger(ROOT / "build" / "smoke_ledger.jsonl"), CallStats()
    try:
        client = CL.make_client()
    except MissingKey as exc:
        print(f"{exc}. Replaying from the smoke cache only.")
        client = None
    for run in (1, 2):
        print(f"\n===== run {run} =====")
        try:
            results = run_smoke(client, cache, ledger, stats, a.dev_emails)
        except LLMCallError as exc:
            print(f"call failed: {type(exc).__name__}: {exc}")
            return 1
        except Exception as exc:   # CacheMiss, BudgetExceeded, ...
            print(f"stopped: {type(exc).__name__}: {exc}")
            return 1
        for r in results:
            print(f"[{r['email_id']}] {r['label']}  model={r['model']}  escalated={r['escalated']}  cache_hit={r['cache_hit']}  "
                  f"latency={r['latency_s']}s  usd={r['usd']}")
            print("  parsed:", json.dumps(r["parsed"], ensure_ascii=False))
            print("  usage: ", r["usage"])
        print(f"real calls so far: {stats.real_calls}   cache hits so far: {stats.cache_hits}   failed attempts: {stats.failed_attempts}")
    print("\n" + cost_from_ledger(ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
