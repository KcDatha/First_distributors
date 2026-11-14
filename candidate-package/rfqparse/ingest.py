"""Read one .eml into a plain object. Stdlib only. Never raises: a broken file comes back
with `error` set (and whatever could still be read).

    from rfqparse.ingest import ingest_eml
    mail = ingest_eml(Path("E003.eml"))
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from .config import RIDGELINE_DOMAIN


@dataclass
class Attachment:
    filename: str | None
    content_type: str
    data: bytes
    inline: bool = False
    content_id: str | None = None

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class Email:
    id: str
    sender: str | None = None          # raw From header
    sender_address: str | None = None  # lowercased address only
    to: str | None = None
    subject: str | None = None
    date: datetime | None = None       # timezone aware, or None if missing/broken
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    hints: dict[str, str | None] = field(default_factory=dict)  # list_unsubscribe, precedence, auto_submitted
    is_ridgeline_sender: bool = False
    text: str = ""
    html: str = ""                     # raw html part(s), kept so inline images can be matched by cid
    current_text: str = ""
    quoted_text: str = ""
    quote_marker: str | None = None    # which rule split the text, and where
    attachments: list[Attachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


# ----------------------------------------------------------------------------- headers


def _hdr(msg: Message, name: str) -> str | None:
    try:
        v = msg.get(name)
        return None if v is None else str(v).strip()
    except Exception:
        return None


def parse_date(value: str | None) -> datetime | None:
    """Aware datetime, or None. A date with no zone info (e.g. `-0000`) is taken as UTC."""
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
    except Exception:
        return None
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def sender_domain(from_header: str | None) -> str | None:
    addr = parseaddr(from_header or "")[1].lower()
    return addr.rsplit("@", 1)[1] if "@" in addr else None


def is_ridgeline(from_header: str | None) -> bool:
    """Exact domain match: a subdomain (e.g. a mail-system host) is not one of our reps."""
    return sender_domain(from_header) == RIDGELINE_DOMAIN.lower()


# ----------------------------------------------------------------------------- html -> text


class _HtmlText(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5",
              "h6", "pre", "hr", "blockquote", "section", "article", "header", "footer"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._cur: list[str] = []
        self._quote = 0
        self._line_quote = 0
        self._skip = 0

    def _flush(self) -> None:
        s = re.sub(r"[ \t\r\f\v ]+", " ", "".join(self._cur)).strip()
        self._cur = []
        if s:
            self.lines.append(("> " * self._line_quote) + s)
        self._line_quote = self._quote

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "blockquote":
            self._flush()
            self._quote += 1
            self._line_quote = self._quote
        elif tag in self._BLOCK:
            self._flush()
        elif tag in ("td", "th") and "".join(self._cur).strip():
            self._cur.append(" | ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "blockquote":
            self._flush()
            self._quote = max(0, self._quote - 1)
            self._line_quote = self._quote
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            if not self._cur:
                self._line_quote = self._quote
            self._cur.append(data)


def html_to_text(html: str) -> str:
    p = _HtmlText()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    p._flush()
    text = "\n".join(p.lines)
    text = re.sub(r"[ \t]*\|[ \t]*\|[ \t]*", " | ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ----------------------------------------------------------------------------- current / quoted split

_WROTE_START = re.compile(r"^\s*On\s", re.I)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.I)
_FROM_LINE = re.compile(r"^\s*From:\s*\S", re.I)
_SENT_LINE = re.compile(r"^\s*(Sent|Date):\s*\S", re.I)
_TO_LINE = re.compile(r"^\s*(To|Subject|Cc):", re.I)
_UNDERSCORES = re.compile(r"^\s*_{5,}\s*$")


def _is_wrote(lines: list[str], i: int) -> bool:
    """`On <date/sender> wrote:` on one line, or wrapped over up to three lines."""
    if not _WROTE_START.match(lines[i]):
        return False
    joined = ""
    for j in range(i, min(i + 3, len(lines))):
        joined = (joined + " " + lines[j].strip()).strip()
        if joined.lower().endswith("wrote:"):
            return len(joined) < 400 and bool(re.search(r"[\d@]", joined))
    return False


def _is_header_block(lines: list[str], i: int) -> bool:
    if not _FROM_LINE.match(lines[i]):
        return False
    nxt = lines[i + 1:i + 6]
    return any(_SENT_LINE.match(l) for l in nxt) and any(_TO_LINE.match(l) for l in nxt)


def split_quoted(text: str) -> tuple[str, str, str | None]:
    """(current, quoted, marker). Everything from the first quote marker to the end is quoted;
    nothing is dropped, so current + quoted always holds the whole text."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i in range(len(lines)):
        kind = None
        if _is_wrote(lines, i):
            kind = "wrote"
        elif _ORIGINAL.match(lines[i]):
            kind = "original-message"
        elif _is_header_block(lines, i):
            kind = "header-block"
            if i > 0 and _UNDERSCORES.match(lines[i - 1]):
                i -= 1
        elif lines[i].lstrip().startswith(">"):
            kind = "angle-quote"
        if kind:
            return ("\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip("\n"), f"{kind}@line {i + 1}")
    return text.strip(), "", None


# ----------------------------------------------------------------------------- MIME walk


def _content(part: Message) -> str:
    try:
        return part.get_content()
    except Exception:
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, "replace")
        except LookupError:
            return raw.decode("utf-8", "replace")


def _walk(part: Message, mail: Email, plain: list[str], html: list[str]) -> None:
    try:
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        if ctype == "message/rfc822":  # kept whole as an attachment, never unpacked into the body
            try:
                inner = part.get_payload(0)
                data = inner.as_bytes() if hasattr(inner, "as_bytes") else (part.get_payload(decode=True) or b"")
            except Exception:
                data = part.get_payload(decode=True) or b""
            mail.attachments.append(Attachment(part.get_filename() or None, ctype, data,
                                               disp == "inline", _cid(part)))
            return
        if part.is_multipart():
            for sub in part.iter_parts():
                _walk(sub, mail, plain, html)
            return
        filename = part.get_filename() or None
        is_text_body = ctype in ("text/plain", "text/html") and disp != "attachment" and not filename
        if is_text_body:
            body = _content(part)
            if body.strip():
                (plain if ctype == "text/plain" else html).append(body)
            return
        data = part.get_payload(decode=True) or b""
        mail.attachments.append(Attachment(filename, ctype, data, disp == "inline", _cid(part)))
    except Exception as exc:
        mail.warnings.append(f"part skipped: {type(exc).__name__}: {exc}")


def _cid(part: Message) -> str | None:
    v = _hdr(part, "Content-ID")
    return v.strip("<>") if v else None


# ----------------------------------------------------------------------------- entry point


def ingest_eml(path: Path | str) -> Email:
    path = Path(path)
    mail = Email(id=path.stem)
    try:
        msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    except Exception as exc:
        mail.error = f"cannot read email: {type(exc).__name__}: {exc}"
        return mail
    try:
        mail.sender = _hdr(msg, "From")
        mail.sender_address = parseaddr(mail.sender or "")[1].lower() or None
        mail.to = _hdr(msg, "To")
        mail.subject = _hdr(msg, "Subject")
        mail.date = parse_date(_hdr(msg, "Date"))
        mail.message_id = _hdr(msg, "Message-ID")
        mail.in_reply_to = _hdr(msg, "In-Reply-To")
        refs = _hdr(msg, "References") or ""
        mail.references = re.findall(r"<[^>]+>", refs) or refs.split()
        mail.hints = {
            "list_unsubscribe": _hdr(msg, "List-Unsubscribe"),
            "precedence": _hdr(msg, "Precedence"),
            "auto_submitted": _hdr(msg, "Auto-Submitted"),
        }
        mail.is_ridgeline_sender = is_ridgeline(mail.sender)

        plain: list[str] = []
        html: list[str] = []
        _walk(msg, mail, plain, html)
        mail.html = "\n\n".join(html)
        if plain:
            mail.text = "\n\n".join(plain)
        elif html:
            mail.text = "\n\n".join(html_to_text(h) for h in html)

        if mail.is_ridgeline_sender:
            # A rep forwarding a customer's email: the forwarded content IS the request.
            mail.current_text, mail.quoted_text, mail.quote_marker = mail.text.strip(), "", None
        else:
            mail.current_text, mail.quoted_text, mail.quote_marker = split_quoted(mail.text)
    except Exception as exc:  # last resort: hand back what we have, flagged
        mail.error = f"partially read: {type(exc).__name__}: {exc}"
    return mail
