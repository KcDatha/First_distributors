"""Dispatcher: one ingested email in, one package out (what the model step will see).

Handlers are picked from the file's actual bytes, not its declared type or extension, because
attachments get mislabeled. Any renderer that raises is caught and becomes an unreadable result.
"""

from __future__ import annotations

import base64
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..ingest import Attachment, Email, html_to_text, ingest_eml
from .csv_ import MAX_TOTAL_CHARS as TEXT_CAP
from .csv_ import decode as decode_text
from .csv_ import render_csv
from .docx import render_docx
from .image import ImageResult, prepare_image
from .pdf import PdfResult, render_pdf
from .xlsx import render_xlsx

MAX_EMBEDDED = 20                 # embedded pictures taken from one docx/xlsx
MAX_EMBEDDED_BYTES = 20_000_000

QUOTED_LABEL = ("QUOTED EARLIER TEXT (history from earlier messages; not part of this request "
                "unless the sender refers to it)")

_EXT_KIND = {".pdf": "pdf", ".xlsx": "xlsx", ".xlsm": "xlsx", ".docx": "docx", ".csv": "csv", ".tsv": "csv",
             ".txt": "text", ".eml": "email", ".xls": "ole", ".doc": "ole", ".ppt": "ole", ".png": "image",
             ".jpg": "image", ".jpeg": "image", ".gif": "image", ".bmp": "image", ".tif": "image",
             ".tiff": "image", ".webp": "image", ".heic": "image", ".heif": "image"}
_MIME_KIND = {"application/pdf": "pdf", "message/rfc822": "email", "text/csv": "csv", "text/plain": "text",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx"}
_UNSUPPORTED = {
    "ole": "legacy Office file (.xls/.doc/.ppt) or a password-protected Office file; it cannot be read",
    "zip": "zip archive; its contents are not unpacked",
    "pptx": "PowerPoint presentation; not supported",
    "xlsb": "binary Excel (.xlsb) workbook; not supported",
    "odf": "OpenDocument file; not supported",
}


@dataclass
class ImageItem:
    label: str                       # where it came from: file name, "page 2", embedded name ...
    result: ImageResult


@dataclass
class AttachmentResult:
    source: str                      # the name to use as a line item's `source`
    declared_type: str
    detected: str                    # kind found from the bytes: pdf xlsx docx csv text html image email ole zip unknown ...
    status: str                      # ok | unreadable | skipped | attached (kept whole, not rendered)
    size: int
    text: str = ""
    images: list[ImageItem] = field(default_factory=list)
    reason: str | None = None
    note: str | None = None          # e.g. "embedded image word/media/image1.png", or a label mismatch
    inline: bool = False
    content_id: str | None = None
    referenced_by_cid: bool = False
    parent: str | None = None        # set on pictures pulled out of a docx/xlsx
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    pdf: PdfResult | None = None     # page classification, for PDFs

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class EmailPackage:
    id: str
    sender: str | None = None
    to: str | None = None
    subject: str | None = None
    date: str | None = None
    is_ridgeline_sender: bool = False
    hints: dict = field(default_factory=dict)
    current_text: str = ""
    quoted_text: str = ""
    attachments: list[AttachmentResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def quoted_labeled(self) -> str:
        return f"[{QUOTED_LABEL}]\n{self.quoted_text}" if self.quoted_text.strip() else ""

    @property
    def image_count(self) -> int:
        return sum(1 for a in self.attachments for i in a.images if i.result.ok)

    @property
    def text_chars(self) -> int:
        return len(self.current_text) + sum(len(a.text) for a in self.attachments)


# ------------------------------------------------------------------ detecting the real type


def _zip_kind(data: bytes) -> str | None:
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except Exception:
        return None
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "word/document.xml" in names:
        return "docx"
    if any(n.startswith("ppt/") for n in names):
        return "pptx"
    if "xl/workbook.bin" in names:
        return "xlsb"
    if "mimetype" in names:
        return "odf"
    return "zip"


def _image_signature(head: bytes) -> bool:
    return (head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff" or head[:4] in (b"GIF8", b"II*\x00", b"MM\x00*")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or head[4:8] == b"ftyp"
            or (head[:2] == b"BM" and len(head) > 14))


def _looks_like_text(data: bytes) -> bool:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or data[:3] == b"\xef\xbb\xbf":
        return True
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    bad = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return bad <= max(2, len(sample) // 50)


def expected_kind(filename: str | None, declared: str) -> str | None:
    return _EXT_KIND.get(Path(filename or "").suffix.lower()) or _MIME_KIND.get((declared or "").lower()) or (
        "image" if (declared or "").lower().startswith("image/") else None)


def detect_kind(data: bytes, filename: str | None, declared: str) -> str:
    head = data[:32]
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        k = _zip_kind(data)
        if k:
            return k
        hint = expected_kind(filename, declared)   # damaged zip: let the matching handler explain the damage
        return hint if hint in ("xlsx", "docx") else "zip"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "ole"
    if _image_signature(head):
        return "image"
    low = data[:2048].lstrip().lower()
    if low.startswith(b"<svg") or (low.startswith(b"<?xml") and b"<svg" in data[:2048].lower()):
        return "image"
    if (declared or "").lower() == "message/rfc822" or Path(filename or "").suffix.lower() == ".eml":
        return "email"
    if data and _looks_like_text(data):
        if Path(filename or "").suffix.lower() in (".csv", ".tsv") or "csv" in (declared or "").lower():
            return "csv"
        if low.startswith((b"<!doctype html", b"<html")) or b"<body" in low[:1024]:
            return "html"
        return "text"
    return "unknown"


# ------------------------------------------------------------------ running one handler


def _safe(name: str, fn, *args):
    """Run a renderer; whatever it does, hand back its result or an unreadable one. Never raises."""
    try:
        return fn(*args)
    except Exception as exc:
        from . import unreadable
        return unreadable(f"{name} renderer failed: {type(exc).__name__}: {exc}")


def _image_attachment(data: bytes, source: str, declared: str, label: str, note: str | None = None,
                      parent: str | None = None, **kw) -> AttachmentResult:
    r = _safe("image", prepare_image, data)
    if not isinstance(r, ImageResult):
        r_status, r_reason, r_text = r.status, r.reason, r.text
        item = None
    else:
        r_status, r_reason, r_text = r.status, r.reason, r.text
        item = ImageItem(label, r)
    return AttachmentResult(source=source, declared_type=declared, detected="image", status=r_status, size=len(data),
                            text=r_text, images=[item] if item else [], reason=None if r_status == "ok" else r_reason,
                            note=note, parent=parent, **kw)


def _embedded_pictures(data: bytes, prefix: str, parent: str) -> tuple[list[AttachmentResult], str]:
    """Pictures stored inside a docx (word/media) or xlsx (xl/media), each sent through the image handler."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = sorted(n for n in z.namelist() if n.startswith(prefix) and not n.endswith("/"))
    except Exception:
        return [], ""
    if not names:
        return [], ""
    out: list[AttachmentResult] = []
    used = 0
    for n in names[:MAX_EMBEDDED]:
        try:
            info = z.getinfo(n)
            if used + info.file_size > MAX_EMBEDDED_BYTES:
                names = names[:len(out)]
                break
            blob = z.read(n)
            used += len(blob)
        except Exception as exc:
            out.append(AttachmentResult(source=parent, declared_type="", detected="image", status="unreadable", size=0,
                                        reason=f"could not extract: {type(exc).__name__}: {exc}",
                                        note=f"embedded image {n}", parent=parent))
            continue
        out.append(_image_attachment(blob, parent, "", n, note=f"embedded image {n}", parent=parent))
    skipped = len(names) - len(out)
    msg = (f"[{len(out)} embedded picture(s) found in {prefix}: each follows as its own attachment with source "
           f"'{parent}' (the note gives the internal name; their position in the document is not known)")
    if skipped > 0:
        msg += f"; {skipped} more not taken (limit {MAX_EMBEDDED} pictures / {MAX_EMBEDDED_BYTES} bytes)"
    return out, msg + "]"


def process_attachment(att: Attachment, referenced_cids: set[str] | None = None) -> list[AttachmentResult]:
    """The attachment's own result first, then any pictures embedded in it."""
    name = att.filename or (f"attachment-{att.content_id}" if att.content_id else "unnamed")
    data = att.data or b""
    declared = att.content_type or ""
    kind = detect_kind(data, att.filename, declared)
    common = dict(inline=att.inline, content_id=att.content_id,
                  referenced_by_cid=bool(att.content_id and att.content_id in (referenced_cids or set())))
    note = None
    exp = expected_kind(att.filename, declared)
    if exp and exp != kind and not ({exp, kind} <= {"csv", "text"}):
        note = f"labelled as {exp} ({declared or 'no type'}, '{name}') but the content is {kind}; read as {kind}"

    children: list[AttachmentResult] = []
    res = AttachmentResult(source=name, declared_type=declared, detected=kind, status="ok", size=len(data),
                           note=note, **common)

    if kind == "pdf":
        r = _safe("pdf", render_pdf, data)
        res.status, res.text, res.reason, res.truncated, res.warnings = r.status, r.text, r.reason, r.truncated, list(r.warnings)
        if isinstance(r, PdfResult):
            res.pdf = r
            for pg in r.pages:
                if pg.png:
                    ir = _safe("image", prepare_image, pg.png)
                    if isinstance(ir, ImageResult):
                        res.images.append(ImageItem(f"page {pg.number} ({pg.kind})", ir))
            if r.truncation_reason:
                res.warnings.append(r.truncation_reason)
    elif kind in ("xlsx", "docx"):
        r = _safe(kind, render_xlsx if kind == "xlsx" else render_docx, data)
        res.status, res.text, res.reason, res.truncated, res.warnings = r.status, r.text, r.reason, r.truncated, list(r.warnings)
        if r.ok:
            children, msg = _embedded_pictures(data, "xl/media/" if kind == "xlsx" else "word/media/", name)
            if msg:
                res.text += "\n" + msg
                res.warnings.append(msg)
    elif kind == "csv":
        r = _safe("csv", render_csv, data)
        res.status, res.text, res.reason, res.truncated, res.warnings = r.status, r.text, r.reason, r.truncated, list(r.warnings)
    elif kind in ("text", "html"):
        try:
            txt, enc = decode_text(data)
            if kind == "html":
                txt = html_to_text(txt)
            txt = txt.strip()
            if not txt:
                res.status, res.reason = "unreadable", "empty file"
            else:
                if len(txt) > TEXT_CAP:
                    txt, res.truncated = txt[:TEXT_CAP] + f"\n[TRUNCATED: text cut at {TEXT_CAP} characters]", True
                res.text = txt
        except Exception as exc:
            res.status, res.reason = "unreadable", f"text renderer failed: {type(exc).__name__}: {exc}"
    elif kind == "image":
        res = _image_attachment(data, name, declared, name, note=note, **common)
    elif kind == "email":
        res.status = "attached"
        res.reason = "message/rfc822 kept as a plain attachment; it is not unpacked into this email"
    elif kind in _UNSUPPORTED:
        res.status, res.reason = "unreadable", _UNSUPPORTED[kind]
    else:
        res.status = "unreadable"
        res.reason = f"unrecognized file type (declared {declared or 'none'}, starts with {data[:8].hex() or 'nothing'})"
    if res.status != "ok" and not res.reason:
        res.reason = "could not be read"
    return [res] + children


# ------------------------------------------------------------------ one email


_CID = re.compile(r"""cid:([^"'\s>)]+)""", re.I)
_DATA_URI = re.compile(r"""data:(image/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)""", re.I)


def render_email(mail: Email) -> EmailPackage:
    pkg = EmailPackage(id=mail.id, sender=mail.sender, to=mail.to, subject=mail.subject,
                       date=mail.date.isoformat() if mail.date else None,
                       is_ridgeline_sender=mail.is_ridgeline_sender, hints=dict(mail.hints),
                       current_text=mail.current_text, quoted_text=mail.quoted_text, error=mail.error)
    pkg.notes.extend(mail.warnings)
    html = getattr(mail, "html", "") or ""
    cids = {c.strip("<>") for c in _CID.findall(html)}
    have = {a.content_id for a in mail.attachments if a.content_id}
    for missing in sorted(cids - have):
        pkg.notes.append(f"the html body references an image 'cid:{missing}' but the email has no such part")
    for att in mail.attachments:
        try:
            pkg.attachments.extend(process_attachment(att, cids))
        except Exception as exc:  # last resort: one attachment never takes the email down
            pkg.attachments.append(AttachmentResult(
                source=att.filename or "unnamed", declared_type=att.content_type or "", detected="unknown",
                status="unreadable", size=len(att.data or b""), reason=f"failed while rendering: {type(exc).__name__}: {exc}"))
    # pictures pasted into the html body as data: URIs (webmail) count as attachments too
    for n, (mime, b64) in enumerate(_DATA_URI.findall(html), 1):
        try:
            blob = base64.b64decode(re.sub(r"\s+", "", b64), validate=False)
        except Exception:
            blob = b""
        pkg.attachments.extend(process_attachment(
            Attachment(f"html-inline-image-{n}", mime, blob, inline=True, content_id=None)))
    return pkg


def render_path(path: Path | str) -> EmailPackage:
    """Ingest and render one .eml. Never raises."""
    path = Path(path)
    try:
        return render_email(ingest_eml(path))
    except Exception as exc:
        return EmailPackage(id=path.stem, error=f"render failed: {type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ the debug view


def format_package(pkg: EmailPackage) -> str:
    """Everything the model would see for this email, as one readable text."""
    L = [f"################ {pkg.id} ################",
         f"From: {pkg.sender}", f"To: {pkg.to}", f"Subject: {pkg.subject}", f"Date: {pkg.date}",
         f"Sender is a Ridgeline rep: {'yes' if pkg.is_ridgeline_sender else 'no'}",
         "Header hints: " + (", ".join(f"{k}={v}" for k, v in pkg.hints.items() if v) or "none")]
    if pkg.error:
        L.append(f"INGEST ERROR: {pkg.error}")
    for n in pkg.notes:
        L.append(f"NOTE: {n}")
    L += ["", "---------------- CURRENT MESSAGE TEXT ----------------", pkg.current_text or "(empty)"]
    L += ["", f"---------------- {QUOTED_LABEL} ----------------", pkg.quoted_text or "(none)"]
    if not pkg.attachments:
        L += ["", "---------------- ATTACHMENTS: none ----------------"]
    for i, a in enumerate(pkg.attachments, 1):
        head = f"---------------- ATTACHMENT {i}: {a.source}  [declared: {a.declared_type or '-'} | detected: {a.detected} | status: {a.status}]"
        L += ["", head + (" (inline)" if a.inline else "") + (" (referenced by cid in html)" if a.referenced_by_cid else "")
              + " ----------------"]
        if a.parent:
            L.append(f"(picture taken from inside {a.parent})")
        if a.note:
            L.append(f"NOTE: {a.note}")
        if a.reason:
            L.append(f"REASON: {a.reason}")
        for w in a.warnings:
            L.append(f"WARNING: {w}")
        if a.pdf:
            L.append(f"PDF pages: total {a.pdf.total_pages}; text {a.pdf.text_pages}; scanned {a.pdf.scanned_pages}; "
                     f"mixed {a.pdf.mixed_pages}")
        if a.text:
            L.append(a.text)
        for it in a.images:
            r = it.result
            if r.ok:
                L.append(f"[IMAGE for vision: {it.label}: {r.width}x{r.height} {r.mime}, {len(r.data)} bytes "
                         f"(original {r.orig_width}x{r.orig_height}, {r.orig_bytes} bytes"
                         f"{'; unchanged' if not r.reencoded else ''}{'; rotated' if r.rotated else ''}"
                         f"{'; downscaled' if r.downscaled else ''})]")
            else:
                L.append(f"[IMAGE NOT SENT: {it.label}: {r.status}: {r.reason}]")
    return "\n".join(L) + "\n"


def summary_line(pkg: EmailPackage) -> str:
    atts = ", ".join(f"{a.source}[{a.detected}]={a.status}" for a in pkg.attachments) or "no attachments"
    att_chars = sum(len(a.text) for a in pkg.attachments)
    return (f"{pkg.id} | {atts} | text: body {len(pkg.current_text)}c, quoted {len(pkg.quoted_text)}c, "
            f"attachments {att_chars}c | images: {pkg.image_count}" + (f" | ERROR {pkg.error}" if pkg.error else ""))
