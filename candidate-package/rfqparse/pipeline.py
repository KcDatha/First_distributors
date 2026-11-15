"""One email in, one parsed.json entry out: ingest, render, build the payload, route and call, validate, map.

Nothing here raises for a bad email, a missing key or a failing provider: every failure becomes a safe
`rfq` entry flagged needs_review with the reason, so the customer is never silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .ingest import Email, ingest_eml
from .llm import cache as llm_cache
from .llm.call import CallStats
from .llm.errors import BudgetExceeded, LLMCallError
from .llm.ledger import Ledger
from .llm.prompts import build_user_parts, system_prompt
from .llm.routing import call_routed
from .llm.schema import EXTRACTION_SCHEMA, REQUEST_CLASSES, SCHEMA_NAME, UOMS
from .render.registry import EmailPackage, render_email


def safe_entry(reason: str) -> dict:
    """What an email gets when nothing better is possible: a request flagged for a human, with the reason."""
    return {"classification": "rfq", "revises": None, "duplicate_of": None, "needs_review": True,
            "review_reason": reason, "line_items": []}


def message_id_index(emails_dir: Path) -> dict[str, str]:
    """Message-ID -> email id for every email in the folder (used to resolve what a revision revises)."""
    idx: dict[str, str] = {}
    for p in sorted(emails_dir.glob("*.eml")):
        try:
            mid = (ingest_eml(p).message_id or "").strip()
        except Exception:
            continue
        if mid:
            idx.setdefault(mid, p.stem)
    return idx


def resolve_revises(mail: Email, index: dict[str, str]) -> str | None:
    """The earlier email this one replies to, if it is in the inbox; else None."""
    refs = [mail.in_reply_to or ""] + list(reversed(mail.references))
    for r in refs:
        r = r.strip()
        if r and r in index and index[r] != mail.id:
            return index[r]
    return None


def make_validator(valid_sources: set[str]):
    """Checks the schema cannot express: every line's source is 'body' or a real attachment name."""
    allowed = {"body"} | set(valid_sources)

    def validate(parsed: dict) -> list[str]:
        problems = []
        for i, ln in enumerate(parsed.get("lines", [])):
            if ln.get("source") not in allowed:
                problems.append(f"lines[{i}].source {ln.get('source')!r} is neither 'body' nor an attachment name")
            if ln.get("uom") is not None and ln["uom"] not in UOMS:
                problems.append(f"lines[{i}].uom {ln['uom']!r} is not an allowed unit")
            q = ln.get("qty")
            if q is not None and (isinstance(q, bool) or not isinstance(q, (int, float))):
                problems.append(f"lines[{i}].qty must be a number or null")
            if not str(ln.get("raw", "")).strip() or not str(ln.get("description", "")).strip():
                problems.append(f"lines[{i}] has an empty raw or description")
        return problems

    return validate


def _num(q):
    return int(q) if isinstance(q, float) and q.is_integer() else q


def map_answer(parsed: dict, pkg: EmailPackage, mail: Email, index: dict[str, str]) -> dict:
    """The model's answer plus the code guards, in the parsed.json shape."""
    cls = parsed["classification"]
    entry = {"classification": cls, "revises": None, "duplicate_of": None, "needs_review": False,
             "review_reason": None, "line_items": []}
    if cls not in REQUEST_CLASSES:
        return entry                          # guard: non-request classes carry no lines and no review flag
    entry["line_items"] = [{"raw": ln["raw"].strip(), "description": ln["description"].strip(), "qty": _num(ln["qty"]),
                            "uom": ln["uom"], "source": ln["source"]} for ln in parsed["lines"]]
    if cls == "rfq_revision":
        entry["revises"] = resolve_revises(mail, index)
    reasons = [f"{d['reason']}: {d['detail'].strip()}" if d["detail"].strip() else d["reason"] for d in parsed["doubts"]]
    for a in pkg.attachments:                 # guard 1: an attachment we could not read, on a request
        if a.status == "unreadable":
            reasons.append(f"attachment '{a.source}' could not be read: {a.reason}")
    if not entry["line_items"] and not reasons:   # guard 2: a request with nothing in it and no flag
        reasons.append("request with no readable line items")
    if reasons:
        entry["needs_review"] = True
        entry["review_reason"] = "; ".join(dict.fromkeys(reasons))   # de-duplicated, order kept
    return entry


def process_email(path: Path, *, client, cache=None, ledger=None, stats: CallStats | None = None,
                  index: dict[str, str] | None = None) -> dict:
    """One .eml to one entry. Never raises."""
    try:
        mail = ingest_eml(path)
        pkg = render_email(mail)
        if mail.error and not (mail.text or mail.attachments):
            return safe_entry(f"email could not be read: {mail.error}")
        if not (mail.sender or mail.subject or mail.to or mail.message_id or mail.date):
            return safe_entry("email could not be read: the file has no email headers (not a valid .eml)")
        user, sources = build_user_parts(pkg, mail)
        result = call_routed(system=system_prompt(), user=user, schema=EXTRACTION_SCHEMA, schema_name=SCHEMA_NAME,
                             client=client, email_id=mail.id, prompt_version=config.PROMPT_VERSION,
                             schema_version=config.SCHEMA_VERSION, validate=make_validator(sources),
                             cache=cache, ledger=ledger, stats=stats)
        return map_answer(result.parsed, pkg, mail, index or {})
    except BudgetExceeded as exc:
        return safe_entry(f"spend limit reached, email not processed: {exc}")
    except llm_cache.CacheMiss:
        return safe_entry("no cached model answer for this email and no model available (no API key)")
    except LLMCallError as exc:
        return safe_entry(f"model call failed: {type(exc).__name__}: {exc}")
    except Exception as exc:
        return safe_entry(f"internal error while processing this email: {type(exc).__name__}")


def run_parse(emails_dir: Path, client, only: list[str] | None = None, cache=None, ledger=None,
              stats: CallStats | None = None) -> dict:
    """Every .eml in the folder (or just `only`) to {id: entry}. Never raises for one bad email."""
    stats = stats if stats is not None else CallStats()
    index = message_id_index(emails_dir)
    out = {}
    for p in sorted(emails_dir.glob("*.eml")):
        if only is not None and p.stem not in only:
            continue
        out[p.stem] = process_email(p, client=client, cache=cache, ledger=ledger, stats=stats, index=index)
    return out


def dumps(result: dict) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False) + "\n"
