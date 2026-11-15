"""One email in, one parsed.json entry out: ingest, render, build the payload, route and call, validate, map.

Nothing here raises for a bad email, a missing key or a failing provider: every failure becomes a safe
`rfq` entry flagged needs_review with the reason, so the customer is never silently dropped.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
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
from .postprocess import completeness as CP
from .postprocess.dupes import find_duplicates
from .render.registry import EmailPackage, render_email


def safe_entry(reason: str) -> dict:
    """What an email gets when nothing better is possible: a request flagged for a human, with the reason."""
    return {"classification": "rfq", "revises": None, "duplicate_of": None, "needs_review": True,
            "review_reason": reason, "line_items": []}


def _ingest_all(emails_dir: Path) -> list[Email]:
    mails = []
    for p in sorted(emails_dir.glob("*.eml")):
        try:
            mails.append(ingest_eml(p))
        except Exception:
            continue
    return mails


def _index_of(mails: list[Email]) -> dict[str, str]:
    idx: dict[str, str] = {}
    for m in mails:
        mid = (m.message_id or "").strip()
        if mid:
            idx.setdefault(mid, m.id)
    return idx


def message_id_index(emails_dir: Path) -> dict[str, str]:
    """Message-ID -> email id for every email in the folder (used to resolve what a revision revises)."""
    return _index_of(_ingest_all(emails_dir))


def duplicate_entry(original: dict, original_id: str) -> dict:
    """A duplicate takes the original's classification, points at it, and carries no lines."""
    return {"classification": original["classification"], "revises": original.get("revises"),
            "duplicate_of": original_id, "needs_review": False, "review_reason": None, "line_items": []}


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


_LEADING_QTY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\b\.?\s*(?:of\s+)?", re.I)


def strip_restated_quantity(description: str, qty, uom) -> str:
    """Remove a leading "<qty> <unit> [of]" from a description, but ONLY when that number equals the line's own qty and the
    unit word stands for the line's own uom (null uom counts as EA). "7 EA 1/2 weld neck" -> "1/2 weld neck"."""
    if qty is None or isinstance(qty, bool):
        return description
    m = _LEADING_QTY_RE.match(description or "")
    if not m:
        return description
    try:
        same_qty = abs(float(m.group(1)) - float(qty)) < 1e-9
    except (TypeError, ValueError):
        return description
    word = m.group(2).lower()
    codes = {code for code, words in config.UNIT_WORDS.items() if word in words}
    if not same_qty or (uom or "EA") not in codes:
        return description
    rest = description[m.end():].strip()
    return rest if rest else description


def phrase_in_text_tokens(phrase: str) -> bool:
    return bool(re.search(r"[a-z0-9]", phrase or "", re.I))


def email_corpus(pkg: EmailPackage) -> str:
    """Everything written in the email that a context phrase may legitimately be copied from."""
    parts = [pkg.subject or "", pkg.current_text, pkg.quoted_text] + [a.text for a in pkg.attachments if a.text and a.detected != "image"]
    return _norm("\n".join(parts))


_DASHES = {ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}
_WORD = re.compile(r"[A-Za-z0-9]+")


def clean_phrase(text: str) -> str:
    """A copied phrase with control and invisible characters replaced by spaces, and unicode dashes made plain."""
    text = (text or "").translate(_DASHES)
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text)
    return " ".join(text.split())


@dataclass
class SpanMatch:
    text: str          # the SOURCE's own text for the matched span, never the model's wording
    score: float       # share of the phrase's words found in the span (1.0 = every word)
    where: str         # which part of the email it came from
    exact: bool        # the span is the phrase word for word


class SourceIndex:
    """The email's own texts (subject, message, quoted text, text attachments), tokenised with character spans so a
    matched window can be returned as the source's original text."""

    def __init__(self, pkg: EmailPackage):
        parts = [("the subject", pkg.subject or ""), ("the message text", pkg.current_text),
                 ("the quoted text", pkg.quoted_text)]
        parts += [(f"attachment '{a.source}'", a.text) for a in pkg.attachments if a.text and a.detected != "image"]
        self.parts = []
        for where, text in parts:
            spans = [(m.group(0).lower(), m.start(), m.end()) for m in _WORD.finditer(text)]
            self.parts.append((where, text, spans))
        self._cache: dict[str, SpanMatch | None] = {}

    def find(self, phrase: str) -> SpanMatch | None:
        """The best window of the email's text for this phrase, if enough of its words are there together. A stray extra
        word, a swapped dash or different punctuation in the copy do not stop it; a phrase with no real counterpart does."""
        phrase = clean_phrase(phrase)
        if phrase in self._cache:
            return self._cache[phrase]
        want = [m.group(0).lower() for m in _WORD.finditer(phrase)]
        best, best_key = None, None
        if want:
            need, wset = Counter(want), set(want)
            lo, hi = max(1, len(want) - config.CONTEXT_FUZZY_EXTRA_WORDS), len(want) + config.CONTEXT_FUZZY_EXTRA_WORDS
            for order, (where, text, spans) in enumerate(self.parts):
                toks = [t for t, _, _ in spans]
                for i, tok in enumerate(toks):
                    if tok not in wset:
                        continue
                    for length in range(lo, hi + 1):
                        j = i + length
                        if j > len(toks):
                            break
                        if toks[j - 1] not in wset:
                            continue
                        end = spans[j - 1][2]
                        while end < len(text) and not text[end].isalnum() and not text[end].isspace() and text[end] not in ",;:.!?":
                            end += 1                      # keep marks stuck to the last word: 3000#, 4", 150#
                        raw_text = text[spans[i][1]:end]
                        if "\n" in raw_text:
                            continue                      # a window never runs across lines
                        score = sum((need & Counter(toks[i:j])).values()) / len(want)
                        shown = " ".join(raw_text.split())
                        # best score first; then a window that reads exactly like the copy (same case and marks); then the
                        # tightest; then the earliest in the email
                        key = (-score, shown != phrase, length, order, i)
                        if best_key is None or key < best_key:
                            best_key = key
                            best = SpanMatch(shown, score, where, toks[i:j] == want)
        result = best if best is not None and best.score >= config.CONTEXT_FUZZY_THRESHOLD else None
        self._cache[phrase] = result
        return result


def apply_line_guards(parsed_lines: list[dict], pkg: EmailPackage, notes: list[str] | None = None) -> list[dict]:
    """Per line: force an unevidenced qty to null; append applies_context phrases missing from the description;
    drop (and note) phrases that are not in the email, so an invented spec cannot get in. Phrases for a line that
    comes from a picture cannot be checked against any text, so they are not added (and that is noted)."""
    notes = notes if notes is not None else []
    index = SourceIndex(pkg)
    # an image attachment's "text" is only a size note, so a line read from a picture is not a text source
    text_sources = {"body"} | {a.source for a in pkg.attachments if a.text and a.detected != "image"}
    out = []
    for i, ln in enumerate(parsed_lines):
        ln = dict(ln)
        tag = f"line {i + 1} ({ln.get('raw', '')[:40]!r})"
        if ln.get("qty") is not None and not has_quantity_evidence(ln.get("raw", "")):
            notes.append(f"{tag}: qty {ln['qty']} set to null (the line has no digit or number word)")
            ln["qty"] = None
        stripped = strip_restated_quantity(ln["description"], ln.get("qty"), ln.get("uom"))
        if stripped != ln["description"]:
            notes.append(f"{tag}: removed the restated quantity and unit from the start of the description")
            ln["description"] = stripped
        desc_norm = _norm(ln["description"])
        add = []
        for given in dict.fromkeys(p.strip() for p in ln.get("applies_context", []) if p.strip()):
            m = index.find(given)
            if m is None:
                notes.append(f"{tag}: dropped context phrase {given!r} (not found in the email text or attachment text)")
                continue
            if ln.get("source") not in text_sources:
                notes.append(f"{tag}: context phrase {given!r} not added (the line comes from a picture, so it cannot be checked)")
                continue
            ph = m.text                                   # always the source's own words, never the model's
            if not m.exact:
                notes.append(f"{tag}: context phrase {given!r} was not in the email word for word; matched the email's own text "
                             f"{ph!r} in {m.where} (word match {m.score:.2f}) and used that")
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
            if ph and phrase_in_text_tokens(ph) and _norm(ph).strip() not in desc_norm and ph not in add:
                add.append(ph)
        if add:
            ln["description"] = ln["description"].rstrip() + ", " + ", ".join(add)
            notes.append(f"{tag}: appended context {add}")
        out.append(ln)
    return out


def make_source_repair(pkg: EmailPackage, notes: list[str] | None = None):
    """A repair step for call_routed: a line whose source is not 'body' or a real attachment/picture name gets one if its raw
    text can be found. Found in the current body or the quoted text: source is 'body'. Found in the text of exactly one
    attachment: that attachment. Found nowhere, or in several attachments, or the raw is too short to mean anything: left
    alone, so validation fails exactly as it would have. A valid source is never touched."""
    notes = notes if notes is not None else []
    valid = {"body"} | {a.source for a in pkg.attachments}
    body = _norm(pkg.current_text + "\n" + pkg.quoted_text)
    att: dict[str, str] = {}
    for a in pkg.attachments:
        if a.text and a.detected != "image":
            att[a.source] = att.get(a.source, " ") + _norm(a.text)

    def repair(parsed: dict) -> dict:
        lines = parsed.get("lines")
        if not isinstance(lines, list):
            return parsed
        out, changed = [], False
        for i, ln in enumerate(lines):
            src = ln.get("source") if isinstance(ln, dict) else None
            if not isinstance(ln, dict) or src in valid:
                out.append(ln)
                continue
            raw = _norm(str(ln.get("raw", "")))
            new = None
            if len(raw.strip()) >= 6 and len(raw.split()) >= 2:
                if raw in body:
                    new, where = "body", "the message text or its quoted text"
                else:
                    hits = [name for name, text in att.items() if raw in text]
                    if len(hits) == 1:
                        new, where = hits[0], f"the text of attachment '{hits[0]}'"
            if new is None:
                out.append(ln)
                continue
            notes.append(f"source repaired on line {i + 1} ({str(ln.get('raw', ''))[:40]!r}): {src!r} -> {new!r} "
                         f"(the line's text was found in {where})")
            out.append({**ln, "source": new})
            changed = True
        return {**parsed, "lines": out} if changed else parsed

    return repair


def _num(q):
    return int(q) if isinstance(q, float) and q.is_integer() else q


def map_answer(parsed: dict, pkg: EmailPackage, mail: Email, index: dict[str, str],
               notes: list[str] | None = None, extra_reasons: list[str] | None = None) -> dict:
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
    reasons += list(extra_reasons or [])
    for a in pkg.attachments:                 # guard 1: an attachment we could not read, on a request
        if a.status == "unreadable":
            reasons.append(f"attachment '{a.source}' could not be read: {a.reason}")
    if not entry["line_items"] and not reasons:   # guard 2: a request with nothing in it and no flag
        reasons.append("request with no readable line items")
    if reasons:
        entry["needs_review"] = True
        entry["review_reason"] = "; ".join(dict.fromkeys(reasons))   # de-duplicated, order kept
    return entry


def second_pass(parsed: dict, pkg: EmailPackage, user: list[dict], sources: set[str], *, client, mail: Email,
                cache, ledger, stats, notes: list[str]) -> tuple[dict, list[str]]:
    """Completeness check on a first-pass answer. If table rows have no returned line, ask once more for just those rows,
    keep only lines that account for them, and merge. Returns (parsed with any new lines, extra review reasons)."""
    rows = CP.candidate_rows(pkg)
    missing = CP.unaccounted(rows, [ln["raw"] for ln in parsed["lines"]])
    if not missing:
        return parsed, []
    asked = missing[:config.COMPLETENESS_MAX_ROWS_ASKED]
    notes.append(f"completeness: {len(missing)} of {len(rows)} table rows had no returned line; second pass asked about {len(asked)}")
    extra: list[str] = []
    merged = dict(parsed)
    try:
        res = call_routed(system=CP_system(), user=list(user) + [{"type": "text", "text": CP.second_pass_text(asked, parsed["classification"])}],
                          schema=EXTRACTION_SCHEMA, schema_name=SCHEMA_NAME + "_second_pass", client=client, email_id=mail.id,
                          prompt_version=config.COMPLETENESS_PROMPT_VERSION, schema_version=config.SCHEMA_VERSION,
                          validate=make_validator(sources), repair=make_source_repair(pkg, notes),
                          cache=cache, ledger=ledger, stats=stats)
        kept, dropped = CP.accept_second_pass(asked, res.parsed["lines"])
        merged["lines"] = list(parsed["lines"]) + kept
        merged["doubts"] = list(parsed["doubts"]) + list(res.parsed["doubts"])
        for ln, why in dropped:
            notes.append(f"completeness: dropped second-pass line {ln.get('raw', '')[:50]!r}: {why}")
        notes.append(f"completeness: second pass ({res.model}) added {len(kept)} line(s)")
    except (llm_cache.CacheMiss, LLMCallError) as exc:
        extra.append(f"{len(missing)} table row(s) have no line and the second pass could not run: {type(exc).__name__}")
        notes.append(f"completeness: second pass failed: {type(exc).__name__}: {exc}")
        return merged, extra
    still = CP.unaccounted(rows, [ln["raw"] for ln in merged["lines"]])
    if still:                                  # includes any rows beyond the ask limit, which were never re-asked
        eg = "; ".join(r.text[:60] for r in still[:3])
        extra.append(f"{len(still)} of {len(rows)} table rows could not be accounted for after a second pass (e.g. {eg})")
        notes.append(f"completeness: still unaccounted: {[r.label for r in still]}")
    return merged, extra


def CP_system() -> str:
    return system_prompt() + CP.SECOND_PASS_ADDENDUM


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
        notes = notes if notes is not None else []
        result = call_routed(system=system_prompt(), user=user, schema=EXTRACTION_SCHEMA, schema_name=SCHEMA_NAME,
                             client=client, email_id=mail.id, prompt_version=config.PROMPT_VERSION,
                             schema_version=config.SCHEMA_VERSION, validate=make_validator(sources),
                             repair=make_source_repair(pkg, notes), cache=cache, ledger=ledger, stats=stats)
        parsed, extra = result.parsed, []
        if parsed["classification"] in REQUEST_CLASSES:
            parsed, extra = second_pass(parsed, pkg, user, sources, client=client, mail=mail, cache=cache, ledger=ledger,
                                        stats=stats, notes=notes)
        return map_answer(parsed, pkg, mail, index or {}, notes, extra)
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
    Duplicates are found in code first and get no model call. `notes` (if given) is filled with
    {id: [what the code guards changed]}."""
    stats = stats if stats is not None else CallStats()
    mails = _ingest_all(emails_dir)
    index = _index_of(mails)
    try:
        dupes = find_duplicates(mails)
    except Exception:
        dupes = {}                       # detection must never take the run down; without it every email is just processed
    paths = {p.stem: p for p in sorted(emails_dir.glob("*.eml"))}
    wanted = [i for i in paths if only is None or i in only]
    need = {i for i in wanted if i not in dupes} | {dupes[i].original for i in wanted if i in dupes}
    entries: dict[str, dict] = {}
    for i in sorted(need):
        if i not in paths:
            continue
        n: list[str] = []
        entries[i] = process_email(paths[i], client=client, cache=cache, ledger=ledger, stats=stats, index=index, notes=n)
        if notes is not None and n and i in wanted:
            notes[i] = n
    out = {}
    for i in wanted:
        if i in dupes and dupes[i].original in entries:
            out[i] = duplicate_entry(entries[dupes[i].original], dupes[i].original)
            if notes is not None:
                notes.setdefault(i, []).insert(0, f"duplicate of {dupes[i].original}: {dupes[i].reason}; no model call")
        elif i in entries:
            out[i] = entries[i]
    return out


def dumps(result: dict) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False) + "\n"
