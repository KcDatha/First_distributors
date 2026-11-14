"""Render an .xlsx (bytes) as text for a model. Requires openpyxl. Never raises.

Output keeps document order, row numbers, notes and headings. Nothing is interpreted: hidden
sheets/rows/columns and struck-through text are labelled, and the model decides what they mean.
"""

from __future__ import annotations

import datetime as _dt
import io
import zipfile

from openpyxl import load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.utils import get_column_letter

from . import RenderResult, unreadable

MAX_BYTES = 15_000_000        # refuse to open anything bigger
MAX_SCAN_ROWS = 20_000        # rows examined per sheet (guards against formatted-but-empty sheets)
MAX_ROWS_PER_SHEET = 1_500    # non-empty rows printed per sheet
MAX_COLS = 40                 # columns printed per row
MAX_CELL_CHARS = 500
MAX_TOTAL_CHARS = 60_000

LEGEND = (
    "[xlsx legend] Rn = spreadsheet row n. Cells are separated by ' | ' in column order (see 'cols'). "
    "[merged] = value copied from a merged cell that starts in an earlier row. "
    "[STRUCK: x] = struck-through text. [HIDDEN ...] = hidden sheet/row/column. "
    "[formula, no cached value: ...] = formula never calculated."
)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    return str(v)


def _clean(s: str) -> str:
    s = " / ".join(p.strip() for p in s.replace("\r", "").split("\n") if p.strip())
    return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + "...[cell cut]"


def _rich(v: CellRichText) -> str:
    out = []
    for part in v:
        if isinstance(part, TextBlock):
            t = str(part.text)
            out.append(f"[STRUCK: {t.strip()}]" if getattr(part.font, "strike", False) and t.strip() else t)
        else:
            out.append(str(part))
    return "".join(out)


def _cell_text(fc, vc) -> str:
    """fc: cell from the formulas workbook (has fonts, formulas); vc: same cell, cached values."""
    f = fc.value
    if isinstance(f, CellRichText):
        text = _rich(f)
        struck = False
    else:
        ftxt = f if isinstance(f, str) and f.startswith("=") else getattr(f, "text", None)
        v = vc.value if vc is not None else None
        if ftxt is not None and v is None:
            text = f"[formula, no cached value: {ftxt}]"
        else:
            text = _fmt(v if ftxt is not None else f)
        struck = bool(fc.font and fc.font.strike)
    text = _clean(text)
    if struck and text:
        text = f"[STRUCK: {text}]"
    if fc.comment is not None and fc.comment.text:
        text += f" [COMMENT: {_clean(fc.comment.text)}]"
    return text


def _hidden_cols(ws) -> set[int]:
    hidden = set()
    for dim in ws.column_dimensions.values():
        if dim.hidden and dim.min and dim.max:
            hidden.update(range(dim.min, min(dim.max, 16384) + 1))
    return hidden


def _merge_map(ws):
    """(row, col) -> (top_row, top_col, multi_row) for every non-top-left cell of a merged range."""
    cover = {}
    for rng in ws.merged_cells.ranges:
        multi = rng.max_row > rng.min_row
        for r in range(rng.min_row, min(rng.max_row, MAX_SCAN_ROWS) + 1):
            for c in range(rng.min_col, min(rng.max_col, MAX_COLS) + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    cover[(r, c)] = (rng.min_row, rng.min_col, multi, rng.min_col)
    return cover


def _render_sheet(ws, wsv, budget_note: list[str]) -> tuple[list[str], bool]:
    lines: list[str] = []
    truncated = False
    state = getattr(ws, "sheet_state", "visible")
    label = f"=== SHEET: {ws.title}"
    if state != "visible":
        label += f"  [HIDDEN SHEET{' (very hidden)' if state == 'veryHidden' else ''}]"
    lines.append(label + " ===")

    max_r = min(ws.max_row or 0, MAX_SCAN_ROWS)
    max_c = min(ws.max_column or 0, MAX_COLS)
    if ws.max_row and ws.max_row > MAX_SCAN_ROWS:
        truncated = True
        lines.append(f"[TRUNCATED: sheet has {ws.max_row} rows, only first {MAX_SCAN_ROWS} scanned]")
    if (ws.max_column or 0) > MAX_COLS:
        truncated = True
        lines.append(f"[TRUNCATED: sheet has {ws.max_column} columns, only first {MAX_COLS} shown]")
    if max_r == 0 or max_c == 0:
        lines.append("(empty sheet)")
        return lines, truncated

    hcols = _hidden_cols(ws)
    cover = _merge_map(ws)
    heads = [get_column_letter(c) + (" [HIDDEN COL]" if c in hcols else "") for c in range(1, max_c + 1)]
    lines.append("cols: " + " | ".join(heads))

    printed = 0
    for r in range(1, max_r + 1):
        cells = []
        for c in range(1, max_c + 1):
            key = (r, c)
            if key in cover:
                tr, tc, multi, first_col = cover[key]
                if multi and r > tr and c == first_col:
                    txt = _cell_text(ws.cell(tr, tc), wsv.cell(tr, tc))
                    txt = f"[merged] {txt}" if txt else ""
                else:
                    txt = ""
            else:
                txt = _cell_text(ws.cell(r, c), wsv.cell(r, c))
            if txt and c in hcols:
                txt = f"[HIDDEN COL] {txt}"
            cells.append(txt)
        if not any(cells):
            continue
        if printed >= MAX_ROWS_PER_SHEET:
            truncated = True
            lines.append(f"[TRUNCATED: more than {MAX_ROWS_PER_SHEET} non-empty rows; rest of sheet not shown]")
            break
        while cells and not cells[-1]:
            cells.pop()
        rdim = ws.row_dimensions.get(r)
        tag = " [HIDDEN ROW]" if rdim is not None and rdim.hidden else ""
        lines.append(f"R{r}{tag}: " + " | ".join(cells))
        printed += 1
    if printed == 0:
        lines.append("(no non-empty cells)")
    return lines, truncated


def render_xlsx(data: bytes) -> RenderResult:
    try:
        return _render(data)
    except Exception as exc:  # belt and braces: the contract is never to raise
        return unreadable(f"unexpected error: {type(exc).__name__}: {exc}")


def _render(data: bytes) -> RenderResult:
    if not data:
        return unreadable("empty file (0 bytes)")
    if len(data) > MAX_BYTES:
        return unreadable(f"file too large ({len(data)} bytes, limit {MAX_BYTES})")
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return unreadable("encrypted or legacy (.xls) Excel file; cannot open as .xlsx")
    if data[:2] != b"PK":
        return unreadable("not an .xlsx file (no zip signature)")
    try:
        wb = load_workbook(io.BytesIO(data), data_only=False, rich_text=True)
        wbv = load_workbook(io.BytesIO(data), data_only=True)
    except zipfile.BadZipFile as exc:
        return unreadable(f"corrupt or truncated .xlsx (bad zip: {exc})")
    except Exception as exc:
        return unreadable(f"cannot open workbook: {type(exc).__name__}: {exc}")

    out: list[str] = [LEGEND]
    warnings: list[str] = []
    truncated = False
    rendered = 0
    tail = ""
    names = list(wb.sheetnames)
    for idx, name in enumerate(names):
        try:
            ws = wb[name]
            if not hasattr(ws, "iter_rows"):  # chartsheet
                out.append(f"=== SHEET: {name}  [chart sheet, no cells] ===")
                continue
            lines, cut = _render_sheet(ws, wbv[name], warnings)
            truncated |= cut
            rendered += 1
        except Exception as exc:
            warnings.append(f"sheet {name!r} unreadable: {type(exc).__name__}: {exc}")
            lines = [f"=== SHEET: {name}  [SHEET UNREADABLE: {type(exc).__name__}] ==="]
        out.extend(lines)
        if sum(len(l) + 1 for l in out) > MAX_TOTAL_CHARS:
            rest = names[idx + 1:]
            if rest:
                tail = f"\n[TRUNCATED: output limit reached; sheets not shown: {', '.join(rest)}]"
            truncated = True
            break

    text = "\n".join(out)
    if len(text) + len(tail) > MAX_TOTAL_CHARS:
        cut = f"\n[TRUNCATED: output cut at {MAX_TOTAL_CHARS} characters]"
        text = text[: max(0, MAX_TOTAL_CHARS - len(tail) - len(cut))] + cut
        truncated = True
    text += tail  # the notice about unshown sheets always survives the hard cut
    if rendered == 0 and not any("chart sheet" in l for l in out):
        return unreadable("no sheet could be read: " + "; ".join(warnings or ["workbook has no sheets"]))
    return RenderResult(status="ok", text=text, truncated=truncated, warnings=warnings)
