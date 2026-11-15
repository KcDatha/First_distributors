"""Duplicate detection, in code, before any model call. Deliberately conservative: wrongly calling a real request a
duplicate loses it silently, which is worse than missing a duplicate.

Two emails are the same request when ALL of these hold:
  * same sender address, and
  * with real attachments: the same set of files by sha256 (inline pictures such as signature logos are ignored),
    and the numbers written in the body are the same set (a changed quantity is not a resend); or
  * with none: the same normalized body, the same normalized quoted text, the same subject once prefixes like
    resend/fwd/re are dropped, and the same inline pictures, with a body long enough to mean something.
The subject alone never decides anything. The email with the later Date header is the duplicate and points to the
earliest email of its group, which is never itself a duplicate. Dates that tie, are missing or cannot be compared mean
nothing is marked.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .. import config
from ..ingest import Email


@dataclass
class DupeMatch:
    original: str
    reason: str


def _squash(text: str) -> str:
    return " ".join((text or "").lower().split())


def normalize_subject(subject: str | None) -> str:
    tags = "|".join(re.escape(p) for p in config.DUP_SUBJECT_PREFIXES)
    s = subject or ""
    while True:
        new = re.sub(rf"^\s*(?:{tags})\s*[:\-]\s*", "", s, flags=re.I)
        if new == s:
            break
        s = new
    return _squash(s)


def _numbers(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"\d+(?:[./-]\d+)*", text or ""))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(mail: Email):
    """(group key, short description of what matched), or None when this email cannot be compared safely."""
    if mail.error or not mail.sender_address or mail.date is None:
        return None
    real = frozenset(_sha(a.data) for a in mail.attachments if not a.inline and a.data)
    inline = frozenset(_sha(a.data) for a in mail.attachments if a.inline and a.data)
    if real:
        key = ("attachments", mail.sender_address, real, _numbers(mail.current_text))
        return key, f"same sender and the same {len(real)} attachment file(s) by sha256"
    body, quoted = _squash(mail.current_text), _squash(mail.quoted_text)
    if len(body) < config.DUP_MIN_BODY_CHARS:
        return None
    key = ("body", mail.sender_address, normalize_subject(mail.subject), body, quoted, inline)
    return key, "same sender, same subject (prefixes ignored) and identical body text"


def find_duplicates(mails: list[Email]) -> dict[str, DupeMatch]:
    """{duplicate email id: DupeMatch(original id, reason)} for the whole inbox."""
    groups: dict = {}
    for m in mails:
        fp = fingerprint(m)
        if fp is not None:
            groups.setdefault(fp[0], []).append((m, fp[1]))
    out: dict[str, DupeMatch] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda x: x[0].date)
        first = members[0][0]
        if members[1][0].date == first.date:
            continue                      # the earliest date is shared: no safe way to say which is the original
        for m, why in members[1:]:
            if m.date > first.date:       # strictly later than the original
                out[m.id] = DupeMatch(first.id, f"{why}; sent {m.date.isoformat()} after the original {first.id} "
                                                f"({first.date.isoformat()})")
    return out
