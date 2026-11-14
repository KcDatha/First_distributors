"""Render a PDF (bytes) per page: text, tables, and a PNG for pages that need vision.

Needs pdfplumber (which brings pdfminer.six and pypdfium2; all pure wheels, no system install).
Never raises: anything that will not open comes back as an unreadable result with the reason.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from .. import config
from . import RenderResult, clean_cell

MAX_BYTES = 25_000_000
MAX_CELL_CHARS = 500
MAX_TOTAL_CHARS = 60_000


@dataclass
class PageInfo:
    number: int                      # 1-based
    kind: str                        # "text", "scanned" (no text layer), "mixed" (text + big image) or "error"
    image_ratio: float = 0.0         # share of the page area covered by images (0..1)
    chars: int = 0                   # non-space text characters found on the page
    images: int = 0                  # embedded images on the page
    tables: int = 0
    png: bytes | None = None         # rasterized page, only for kind == "scanned"
    error: str | None = None


@dataclass
class PdfResult(RenderResult):
    pages: list[PageInfo] = field(default_factory=list)
    total_pages: int = 0             # pages in the file (may exceed len(pages) when capped)
    truncation_reason: str | None = None

    @property
    def text_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.kind == "text"]

    @property
    def scanned_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.kind == "scanned"]

    @property
    def mixed_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.kind == "mixed"]

    @property
    def vision_pages(self) -> list[int]:
        """Pages that carry a PNG for the model: scanned and mixed."""
        return [p.number for p in self.pages if p.png is not None]


def _unreadable(reason: str) -> PdfResult:
    return PdfResult(status="unreadable", reason=reason)


def _explain(exc: BaseException) -> str:
    """Name what actually went wrong, unwrapping pdfplumber's wrapper exception."""
    inner = exc.args[0] if exc.args and isinstance(exc.args[0], BaseException) else exc
    name = type(inner).__name__
    msg = str(inner).strip()
    low = f"{name} {msg}".lower()
    if "password" in low or "encrypt" in low or "decrypt" in low:
        return f"PDF is password-protected or encrypted ({name}); cannot be read without the password"
    if "syntax" in low or "eof" in low or "xref" in low or "trailer" in low or "no /root" in low or "catalog" in low:
        return f"corrupt or truncated PDF ({name}: {msg or 'malformed structure'})"
    return f"cannot open PDF ({name}: {msg or 'no detail'})"


def render_pdf(data: bytes) -> PdfResult:
    try:
        return _render(data)
    except BaseException as exc:  # contract: never raise (KeyboardInterrupt excepted below)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return _unreadable(f"unexpected error: {_explain(exc)}")


def _center_in(obj, bbox) -> bool:
    x0, top, x1, bottom = bbox
    cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def _page_blocks(page) -> tuple[list[str], int, int]:
    """(lines in reading order, table count, non-space char count). Table text appears once,
    as rows, and is removed from the running text."""
    chars = sum(1 for c in page.chars if (c.get("text") or "").strip())
    tables = []
    try:
        tables = page.find_tables()
    except Exception:
        tables = []
    boxes = [t.bbox for t in tables]
    body = page
    if boxes:
        body = page.filter(lambda o: not (o.get("object_type") == "char" and any(_center_in(o, b) for b in boxes)))
    blocks: list[tuple[float, list[str]]] = []
    try:
        for ln in body.extract_text_lines(return_chars=False):
            if ln["text"].strip():
                blocks.append((ln["top"], [ln["text"].strip()]))
    except Exception:
        txt = body.extract_text() or ""
        if txt.strip():
            blocks.append((0.0, [l for l in txt.splitlines() if l.strip()]))
    for n, t in enumerate(tables, 1):
        rows = []
        for r, row in enumerate(t.extract() or [], 1):
            cells = [clean_cell(c or "", MAX_CELL_CHARS) for c in row]
            while cells and not cells[-1]:
                cells.pop()
            if cells:
                rows.append(f"R{r}: " + " | ".join(cells))
        blocks.append((t.bbox[1], [f"[TABLE {n}]"] + rows))
    blocks.sort(key=lambda b: b[0])
    return [l for _, ls in blocks for l in ls], len(tables), chars


def _image_ratio(page) -> float:
    """Share of the page covered by images (each clipped to the page; overlaps counted twice, capped at 1)."""
    area = float(page.width) * float(page.height)
    if area <= 0:
        return 0.0
    covered = 0.0
    for im in page.images:
        w = min(im["x1"], page.width) - max(im["x0"], 0)
        h = min(im["bottom"], page.height) - max(im["top"], 0)
        if w > 0 and h > 0:
            covered += w * h
    return min(1.0, covered / area)


def _rasterize(page) -> bytes:
    img = page.to_image(resolution=config.PDF_RASTER_DPI)
    buf = io.BytesIO()
    img.original.save(buf, format="PNG")
    return buf.getvalue()


def _render(data: bytes) -> PdfResult:
    import pdfplumber  # imported here so a missing install is a typed result, not an import crash

    if not data:
        return _unreadable("empty file (0 bytes)")
    if len(data) > MAX_BYTES:
        return _unreadable(f"file too large ({len(data)} bytes, limit {MAX_BYTES})")
    if b"%PDF-" not in data[:1024]:
        return _unreadable("not a PDF (no %PDF header)")

    warnings: list[str] = []
    if b"%%EOF" not in data[-2048:]:
        warnings.append("no %%EOF marker at the end: the file may be truncated")
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return _unreadable(_explain(exc))

    with pdf:
        try:
            total = len(pdf.pages)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return _unreadable(_explain(exc))
        if total == 0:
            why = "PDF has no readable pages" + (" (file looks truncated)" if warnings else "")
            return _unreadable(why)

        cap = config.PDF_MAX_PAGES
        result = PdfResult(status="ok", total_pages=total, warnings=warnings)
        out: list[str] = []
        for idx in range(min(total, cap)):
            n = idx + 1
            try:
                page = pdf.pages[idx]
                lines, ntables, chars = _page_blocks(page)
                ratio = _image_ratio(page)
                info = PageInfo(n, "text", chars=chars, images=len(page.images), tables=ntables, image_ratio=ratio)
                if chars < config.PDF_SCAN_MIN_CHARS:
                    info.kind = "scanned"
                elif ratio >= config.PDF_MIXED_IMAGE_RATIO:
                    info.kind = "mixed"
                if info.kind != "text":
                    try:
                        info.png = _rasterize(page)
                    except Exception as exc:
                        info.error = f"needs vision but could not be rasterized: {type(exc).__name__}: {exc}"
                        warnings.append(f"page {n}: {info.error}")
                        if info.kind == "scanned":
                            info.kind = "error"
                attached = "image attached for vision" if info.png else "NO IMAGE AVAILABLE"
                if info.kind in ("scanned", "error"):
                    out.append(f"--- Page {n} of {total}: SCANNED, almost no text layer ({chars} chars); {attached} ---")
                elif info.kind == "mixed":
                    out.append(f"--- Page {n} of {total}: MIXED, text layer ({chars} chars) plus images covering "
                               f"{ratio:.0%} of the page; {attached}; the text below may be only part of the page ---")
                else:
                    out.append(f"--- Page {n} of {total} ---")
                out.extend(lines)
            except Exception as exc:
                info = PageInfo(n, "error", error=f"{type(exc).__name__}: {exc}")
                warnings.append(f"page {n} unreadable: {info.error}")
                out.append(f"--- Page {n} of {total}: UNREADABLE ({type(exc).__name__}) ---")
            result.pages.append(info)
            if page_cache_clear := getattr(pdf.pages[idx], "flush_cache", None):
                page_cache_clear()

    if all(p.kind == "error" for p in result.pages):
        return _unreadable("no page could be read: " + "; ".join(warnings))
    if total > cap:
        result.truncated = True
        result.truncation_reason = f"PDF has {total} pages; only the first {cap} were read"
        out.append(f"[TRUNCATED: {result.truncation_reason}]")
    text = "\n".join(out)
    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS] + f"\n[TRUNCATED: output cut at {MAX_TOTAL_CHARS} characters]"
        result.truncated = True
        result.truncation_reason = (result.truncation_reason + "; " if result.truncation_reason else "") + \
            f"text cut at {MAX_TOTAL_CHARS} characters"
    result.text = text
    return result
