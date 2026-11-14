"""Render a CSV (bytes) as text for a model. Stdlib only. Never raises."""

from __future__ import annotations

import csv
import io

from . import RenderResult, clean_cell, unreadable

MAX_BYTES = 15_000_000
MAX_ROWS = 1_500          # non-empty records printed
MAX_COLS = 40
MAX_CELL_CHARS = 500
MAX_TOTAL_CHARS = 60_000
DELIMITERS = [",", ";", "\t", "|"]


def decode(data: bytes) -> tuple[str, str]:
    """(text, encoding used). UTF-8 (BOM allowed) first, then UTF-16 if it has a BOM, then cp1252,
    and latin-1 as the last resort (it cannot fail)."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeError:
            pass
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc), enc
        except UnicodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def sniff_delimiter(text: str) -> str:
    """The candidate that splits the most of the first lines into the same (>1) number of fields."""
    lines = [l for l in text.splitlines()[:30] if l.strip()]
    best, best_score = ",", 0
    for d in DELIMITERS:
        counts = [l.count(d) for l in lines]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        score = counts.count(mode) if mode > 0 else 0
        if score > best_score:
            best, best_score = d, score
    return best


def render_csv(data: bytes) -> RenderResult:
    try:
        return _render(data)
    except Exception as exc:
        return unreadable(f"unexpected error: {type(exc).__name__}: {exc}")


def _render(data: bytes) -> RenderResult:
    if not data or not data.strip():
        return unreadable("empty file")
    if len(data) > MAX_BYTES:
        return unreadable(f"file too large ({len(data)} bytes, limit {MAX_BYTES})")
    if b"\x00" in data and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        return unreadable("binary file (contains NUL bytes), not a text CSV")
    text, enc = decode(data)
    if not text.strip():
        return unreadable("empty file")
    delim = sniff_delimiter(text)
    warnings = []
    if enc not in ("utf-8-sig",):
        warnings.append(f"decoded as {enc}, not UTF-8")

    shown = ["[csv] Rn = record n of the file (header row included). Cells are separated by ' | '. "
             f"Delimiter detected: {'TAB' if delim == chr(9) else repr(delim)}."]
    truncated = False
    printed = 0
    total = len(shown[0])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    n = 0
    try:
        for row in reader:
            n += 1
            cells = [clean_cell(c.strip(), MAX_CELL_CHARS) for c in row]
            if len(cells) > MAX_COLS:
                cells = cells[:MAX_COLS]
                if "columns cut" not in " ".join(warnings):
                    warnings.append(f"columns cut at {MAX_COLS}")
                    truncated = True
            while cells and not cells[-1]:
                cells.pop()
            if not cells:
                continue
            line = f"R{n}: " + " | ".join(cells)
            if printed >= MAX_ROWS or total + len(line) + 1 > MAX_TOTAL_CHARS:
                shown.append(f"[TRUNCATED: output limit reached at record {n}; rest of file not shown]")
                truncated = True
                break
            shown.append(line)
            total += len(line) + 1
            printed += 1
    except csv.Error as exc:
        shown.append(f"[CSV PARSE ERROR at record {n + 1}: {exc}; rest of file not shown]")
        warnings.append(f"csv parse error at record {n + 1}: {exc}")
        truncated = True
    if printed == 0:
        return unreadable("no non-empty rows")
    return RenderResult(status="ok", text="\n".join(shown), truncated=truncated, warnings=warnings)
