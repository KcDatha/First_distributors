"""One email in, one parsed.json entry out: ingest, render, build the payload, route and call, validate, map.

Nothing here raises for a bad email, a missing key or a failing provider: every failure becomes a safe
`rfq` entry flagged needs_review with the reason, so the customer is never silently dropped.
"""

from __future__ import annotations

import json
import re
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


# ---- code guards that run after the call

_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
                 "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred hundreds "
                 "thousand dozen dozens gross pair pairs single half").split()
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.I)


# "a" / "an" before an ordinary word means one ("a gate valve"), but not before a vague word ("a couple of caps").
_ARTICLE_ONE_RE = re.compile(r"\b(?:a|an)\s+(?!(?:couple|few|several|bunch|handful|lot)\b)[a-z]", re.I)


def has_quantity_evidence(raw: str) -> bool:
    """True when the line's text has a digit, a number word, or a standalone "a"/"an" meaning one."""
    return bool(re.search(r"\d", raw or "") or _NUMBER_WORD_RE.search(raw or "") or _ARTICLE_ONE_RE.search(raw or ""))


def _family_matches(text: str, family: dict) -> list[tuple[str, str]]:
    """[(matched text, normalized value)] for one attribute family."""
    out = []
    for pat in family["patterns"]:
        for m in re.finditer(pat, text or "", re.I):
            out.append((m.group(0), m.group(1).lower()))
    return out


def conflicting_values(phrase: str, raw: str, description: str) -> list[tuple[str, str]]:
    """[(family name, matched text in the phrase)] where the phrase states a value that differs from the value the
    line states itself (its raw text first, else its description). A family the line does not state never conflicts."""
    found = []
    for fam in config.CONTEXT_CONFLICT_FAMILIES:
        in_phrase = _family_matches(phrase, fam)
        if not in_phrase:
            continue
        own = {v for _, v in _family_matches(raw, fam)} or {v for _, v in _family_matches(description, fam)}
        if own and not ({v for _, v in in_phrase} & own):
            found += [(fam["name"], text) for text, v in in_phrase if v not in own]
    return found


def trim_conflicts(phrase: str, conflicts: list[tuple[str, str]]) -> str:
    """The phrase with the conflicting values cut out (for CONTEXT_CONFLICT_ACTION = "trim")."""
    for _, text in conflicts:
        phrase = phrase.replace(text, " ")
    return re.sub(r"\s{2,}", " ", re.sub(r"\s*[,;]\s*(?=[,;]|$)", "", phrase)).strip(" ,;-")


def _norm(text: str) -> str:
    """Lowercase, punctuation to single spaces, padded so a phrase must match whole words."""
    return " " + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower())) + " "


def phrase_in_text(phrase: str, norm_corpus: str) -> bool:
    n = _norm(phrase)
    return n.strip() != "" and n in norm_corpus


def phrase_in_text_tokens(phrase: str) -> bool:
    return bool(re.search(r"[a-z0-9]", phrase or "", re.I))


def email_corpus(pkg: EmailPackage) -> str:
    """Everything written in the email that a context phrase may legitimately be copied from."""
    parts = [pkg.subject or "", pkg.current_text, pkg.quoted_text] + [a.text for a in pkg.attachments if a.text and a.detected != "image"]
    return _norm("\n".join(parts))


def apply_line_guards(parsed_lines: list[dict], pkg: EmailPackage, notes: list[str] | None = None) -> list[dict]:
    """Per line: force an unevidenced qty to null; append applies_context phrases missing from the description;
    drop (and note) phrases that are not in the email, so an invented spec cannot get in. Phrases for a line that
    comes from a picture cannot be checked against any text, so they are not added (and that is noted)."""
    notes = notes if notes is not None else []
    corpus = email_corpus(pkg)
    # an image attachment's "text" is only a size note, so a line read from a picture is not a text source
    text_sources = {"body"} | {a.source for a in pkg.attachments if a.text and a.detected != "image"}
    out = []
    for i, ln in enumerate(parsed_lines):
        ln = dict(ln)
        tag = f"line {i + 1} ({ln.get('raw', '')[:40]!r})"
        if ln.get("qty") is not None and not has_quantity_evidence(ln.get("raw", "")):
            notes.append(f"{tag}: qty {ln['qty']} set to null (the line has no digit or number word)")
            ln["qty"] = None
        desc_norm = _norm(ln["description"])
        add = []
        for ph in dict.fromkeys(p.strip() for p in ln.get("applies_context", []) if p.strip()):
            if not phrase_in_text(ph, corpus):
                notes.append(f"{tag}: dropped context phrase {ph!r} (not found in the email text or attachment text)")
            elif ln.get("source") not in text_sources:
                notes.append(f"{tag}: context phrase {ph!r} not added (the line comes from a picture, so it cannot be checked)")
            else:
                bad = conflicting_values(ph, ln.get("raw", ""), ln["description"])
                if bad:
                    what = ", ".join(f"{name} {text!r}" for name, text in bad)
                    if config.CONTEXT_CONFLICT_ACTION == "trim":
                        kept = trim_conflicts(ph, bad)
                        notes.append(f"{tag}: context phrase {ph!r} states a different {what} than the line; trimmed to {kept!r}")
                        ph = kept
                    else:
                        notes.append(f"{tag}: skipped context phrase {ph!r} (it states a different {what} than the line itself)")
                        continue
                if ph and phrase_in_text_tokens(ph) and _norm(ph).strip() not in desc_norm:
                    add.append(ph)
        if add:
            ln["description"] = ln["description"].rstrip() + ", " + ", ".join(add)
            notes.append(f"{tag}: appended context {add}")
        out.append(ln)
    return out


def _num(q):
    return int(q) if isinstance(q, float) and q.is_integer() else q


def map_answer(parsed: dict, pkg: EmailPackage, mail: Email, index: dict[str, str],
               notes: list[str] | None = None) -> dict:
    """The model's answer plus the code guards, in the parsed.json shape."""
    cls = parsed["classification"]
    entry = {"classification": cls, "revises": None, "duplicate_of": None, "needs_review": False,
             "review_reason": None, "line_items": []}
    if cls not in REQUEST_CLASSES:
        return entry                          # guard: non-request classes carry no lines and no review flag
    guarded = apply_line_guards(parsed["lines"], pkg, notes)
    entry["line_items"] = [{"raw": ln["raw"].strip(), "description": ln["description"].strip(), "qty": _num(ln["qty"]),
                            "uom": ln["uom"], "source": ln["source"]} for ln in guarded]
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
                  index: dict[str, str] | None = None, notes: list[str] | None = None) -> dict:
    """One .eml to one entry. Never raises. `notes` (if given) collects what the code guards changed."""
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
        return map_answer(result.parsed, pkg, mail, index or {}, notes)
    except BudgetExceeded as exc:
        return safe_entry(f"spend limit reached, email not processed: {exc}")
    except llm_cache.CacheMiss:
        return safe_entry("no cached model answer for this email and no model available (no API key)")
    except LLMCallError as exc:
        return safe_entry(f"model call failed: {type(exc).__name__}: {exc}")
    except Exception as exc:
        return safe_entry(f"internal error while processing this email: {type(exc).__name__}")


def run_parse(emails_dir: Path, client, only: list[str] | None = None, cache=None, ledger=None,
              stats: CallStats | None = None, notes: dict[str, list[str]] | None = None) -> dict:
    """Every .eml in the folder (or just `only`) to {id: entry}. Never raises for one bad email.
    `notes` (if given) is filled with {id: [what the code guards changed]}."""
    stats = stats if stats is not None else CallStats()
    index = message_id_index(emails_dir)
    out = {}
    for p in sorted(emails_dir.glob("*.eml")):
        if only is not None and p.stem not in only:
            continue
        n: list[str] = []
        out[p.stem] = process_email(p, client=client, cache=cache, ledger=ledger, stats=stats, index=index, notes=n)
        if notes is not None and n:
            notes[p.stem] = n
    return out


def dumps(result: dict) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False) + "\n"
