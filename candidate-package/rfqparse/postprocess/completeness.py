"""Completeness check: did the model return a line for every table row that looks like an item?

Table rows come from the renderers ("Rn: a | b | c"). A row counts as a candidate item row when it has several
non-empty cells and a digit, and is not a total/footer row, not struck through, not hidden, and not on a hidden sheet.
A returned line accounts for a row when their raw texts overlap enough (token overlap, the same idea the scorer uses),
one line per row. Pure functions; no model calls here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config
from ..render.registry import EmailPackage

_ROW = re.compile(r"^(?:\[TEXT BOX\]\s*)?R(\d+)( \[HIDDEN ROW\])?: ?(.*)$")


@dataclass
class TableRow:
    source: str          # attachment the row was rendered from
    section: str         # sheet, table or page it sits in
    number: int          # the renderer's row number
    text: str            # cells joined with " | ", rendering tags removed

    @property
    def label(self) -> str:
        return f"{self.source} / {self.section} / R{self.number}"


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _is_total(cells: list[str]) -> bool:
    words = "|".join(re.escape(w) for w in config.COMPLETENESS_TOTAL_WORDS)
    pat = re.compile(rf"(?<![a-z0-9])(?:{words})(?![a-z0-9])", re.I)
    return any(pat.search(c) for c in cells)


def _clean_cell(c: str) -> str:
    return re.sub(r"^\[merged\]\s*", "", c.strip())


def candidate_rows(pkg: EmailPackage) -> list[TableRow]:
    """Every table row in the rendered attachments that should have produced a line."""
    out: list[TableRow] = []
    for a in pkg.attachments:
        if a.status != "ok" or not a.text or a.detected == "image":
            continue
        section, hidden_sheet = "", False
        for line in a.text.splitlines():
            if line.startswith("=== SHEET:"):
                section, hidden_sheet = line.strip("= ").replace("SHEET: ", ""), "[HIDDEN SHEET" in line
                continue
            if line.startswith("[TABLE") or line.startswith("--- Page"):
                section, hidden_sheet = line.strip("-[] ").split("]")[0], False
                continue
            m = _ROW.match(line)
            if not m or hidden_sheet or m.group(2):
                continue
            cells = [_clean_cell(c) for c in m.group(3).split(" | ")]
            filled = [c for c in cells if c]
            if len(filled) < config.COMPLETENESS_MIN_CELLS or not any(ch.isdigit() for c in filled for ch in c):
                continue                                       # a heading, a note or a header row
            if all(c.startswith("[STRUCK:") for c in filled) or _is_total(filled):
                continue
            out.append(TableRow(a.source, section, int(m.group(1)), " | ".join(filled)))
    return out


def match(rows: list[TableRow], raws: list[str]) -> dict[int, int]:
    """{row index: line index}: best overlaps first, one line per row, only above the threshold."""
    rt = [tokens(r.text) for r in rows]
    lt = [tokens(r) for r in raws]
    cands = sorted(((-overlap(a, b), i, j) for i, a in enumerate(rt) for j, b in enumerate(lt)
                    if overlap(a, b) >= config.COMPLETENESS_MATCH_THRESHOLD))
    used_r: set[int] = set()
    used_l: set[int] = set()
    out: dict[int, int] = {}
    for _, i, j in cands:
        if i not in used_r and j not in used_l:
            used_r.add(i)
            used_l.add(j)
            out[i] = j
    return out


def unaccounted(rows: list[TableRow], raws: list[str]) -> list[TableRow]:
    matched = match(rows, raws)
    return [r for i, r in enumerate(rows) if i not in matched]


def accept_second_pass(asked: list[TableRow], new_lines: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Keep only second-pass lines that account for one of the rows we asked about. Anything else (a total row, a line
    already returned, something invented) is dropped with a reason."""
    matched = match(asked, [ln.get("raw", "") for ln in new_lines])
    keep_idx = set(matched.values())
    kept = [ln for j, ln in enumerate(new_lines) if j in keep_idx]
    dropped = [(ln, "does not match any row that was asked about (a total, a line already returned, or invented)")
               for j, ln in enumerate(new_lines) if j not in keep_idx]
    return kept, dropped


SECOND_PASS_ADDENDUM = """\

SECOND PASS
You are checking an earlier answer for this same email (the full email is above). The earlier answer returned lines but left out the table rows listed in the final part of the user message. For each listed row that is a real item the customer is asking for, return one line for it, following every rule above (raw is the row's cells as shown, the description carries the context that applies to it, qty and uom as stated, source is the attachment named in the row's label, applies_context as before). Return NOTHING for a listed row that is a heading, note, total, subtotal, price or anything else that is not an item. Do not return any line for a row that is not listed. Set classification to the value given. If none of the listed rows is a real item, return an empty lines list."""


def second_pass_text(asked: list[TableRow], classification: str) -> str:
    rows = "\n".join(f"- [{r.label}] {r.text}" for r in asked)
    return (f"=== SECOND PASS REQUEST ===\nclassification (as already decided): {classification}\n"
            f"Table rows with no matching line in the earlier answer ({len(asked)}):\n{rows}")
