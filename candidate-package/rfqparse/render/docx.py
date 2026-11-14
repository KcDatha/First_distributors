"""Render a .docx (bytes) as text for a model. Stdlib only (zipfile + xml.etree). Never raises.

Body is walked in document order, so paragraphs and tables stay where they were. Nothing is
interpreted: headings, list items, struck text and tracked deletions are labelled for the model.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile

from . import RenderResult, clean_cell, unreadable

MAX_BYTES = 15_000_000
MAX_PART_BYTES = 30_000_000      # uncompressed size of any one XML part (zip-bomb guard)
MAX_TOTAL_CHARS = 60_000
MAX_CELL_CHARS = 500
RESERVE_FOR_EDGES = 3_000        # room kept for footers/footnotes when the body is cut

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"

LEGEND = ("[docx legend] [HEADING n] = heading style. '- ' = list item (indented by level). "
          "[TABLE] rows are 'Rn: cell | cell'. [merged] = value carried down from a merged cell. "
          "[TEXT BOX] = text inside a text box. [STRUCK: x] = struck-through text or a tracked deletion. Tracked insertions appear as normal text.")


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _textboxes(el):
    """w:txbxContent elements under el. Word stores each text box twice (mc:Choice and an
    mc:Fallback copy for old readers); only the Choice copy is read, so nothing prints twice."""
    for ch in el:
        if ch.tag == f"{{{MC}}}Fallback":
            continue
        if ch.tag == q("txbxContent"):
            yield ch
        else:
            yield from _textboxes(ch)


def _val(el, default=None):
    return el.get(q("val"), default) if el is not None else default


def _on(el) -> bool:
    """A w:strike-style toggle: present means on unless val says off."""
    return el is not None and _val(el, "1") not in ("0", "false", "off")


# ------------------------------------------------------------------ paragraphs


def _segments(p) -> list[tuple[str, bool]]:
    segs: list[tuple[str, bool]] = []

    def run(r, deleted):
        rpr = r.find(q("rPr"))
        struck = deleted or (rpr is not None and (_on(rpr.find(q("strike"))) or _on(rpr.find(q("dstrike")))))
        parts = []
        for c in r:
            if c.tag in (q("t"), q("delText")):
                parts.append(c.text or "")
            elif c.tag in (q("tab"), q("br"), q("cr")):
                parts.append(" ")
            elif c.tag == q("noBreakHyphen"):
                parts.append("-")
        if parts:
            segs.append(("".join(parts), struck))

    def walk(el, deleted):
        for ch in el:
            if ch.tag == q("r"):
                run(ch, deleted)
            elif ch.tag in (q("del"), q("moveFrom")):
                walk(ch, True)
            elif ch.tag in (q("ins"), q("moveTo"), q("hyperlink"), q("smartTag"), q("fldSimple"),
                            q("sdt"), q("sdtContent")):
                walk(ch, deleted)

    walk(p, False)
    merged: list[tuple[str, bool]] = []
    for t, s in segs:
        if merged and merged[-1][1] == s:
            merged[-1] = (merged[-1][0] + t, s)
        else:
            merged.append((t, s))
    return merged


def _paragraph_text(p) -> str:
    out = []
    for t, struck in _segments(p):
        if struck:
            inner = t.strip()
            if inner:
                lead = " " if t[:1].isspace() else ""
                trail = " " if t[-1:].isspace() else ""
                out.append(f"{lead}[STRUCK: {inner}]{trail}")
        else:
            out.append(t)
    return re.sub(r"[ \t ]+", " ", "".join(out)).strip()


def _style_names(styles_root) -> dict[str, tuple[str, str | None]]:
    out = {}
    if styles_root is None:
        return out
    for st in styles_root.iter(q("style")):
        sid = st.get(q("styleId"))
        name = st.find(q("name"))
        based = st.find(q("basedOn"))
        if sid:
            out[sid] = (_val(name, "") or "", _val(based))
    return out


def _heading_label(p, styles) -> str | None:
    ppr = p.find(q("pPr"))
    if ppr is None:
        return None
    sid = _val(ppr.find(q("pStyle")))
    for _ in range(6):  # follow basedOn a few levels
        if not sid or sid not in styles:
            break
        name, based = styles[sid]
        low = name.lower().strip()
        m = re.match(r"(?:heading|titre|überschrift)\s*(\d+)", low)
        if m:
            return f"HEADING {m.group(1)}"
        if low == "title":
            return "TITLE"
        if low == "subtitle":
            return "SUBTITLE"
        sid = based
    m = re.match(r"(?i)heading\s*(\d+)$", sid or "")
    if m:
        return f"HEADING {m.group(1)}"
    ol = ppr.find(q("outlineLvl"))
    if ol is not None and (_val(ol) or "").isdigit() and int(_val(ol)) < 9:
        return f"HEADING {int(_val(ol)) + 1}"
    return None


def _list_prefix(p) -> str | None:
    ppr = p.find(q("pPr"))
    num = ppr.find(q("numPr")) if ppr is not None else None
    if num is None:
        return None
    if _val(num.find(q("numId")), "0") == "0":
        return None
    lvl = _val(num.find(q("ilvl")), "0")
    return "  " * (int(lvl) if str(lvl).isdigit() else 0) + "- "


# ------------------------------------------------------------------ tables


def _cell_text(tc, styles) -> str:
    parts = []
    for ch in tc:
        if ch.tag == q("p"):
            t = _paragraph_text(ch)
            if t:
                parts.append(t)
        elif ch.tag == q("tbl"):
            rows = [" | ".join(r) for r in _table_rows(ch, styles)]
            if rows:
                parts.append("[nested table: " + " ; ".join(rows) + "]")
        elif ch.tag == q("sdt"):
            sc = ch.find(q("sdtContent"))
            if sc is not None:
                t = _cell_text(sc, styles)
                if t:
                    parts.append(t)
    return clean_cell(" / ".join(parts), MAX_CELL_CHARS)


def _table_rows(tbl, styles) -> list[list[str]]:
    rows: list[list[str]] = []
    carried: dict[int, str] = {}  # column index -> text of the open vertical merge
    for tr in tbl.findall(q("tr")):
        cells: list[str] = []
        col = 0
        for tc in tr.findall(q("tc")):
            tcpr = tc.find(q("tcPr"))
            span = 1
            vm = None
            if tcpr is not None:
                gs = tcpr.find(q("gridSpan"))
                if gs is not None and (_val(gs) or "").isdigit():
                    span = max(1, int(_val(gs)))
                vm = tcpr.find(q("vMerge"))
            text = _cell_text(tc, styles)
            if vm is not None:
                if _val(vm) == "restart":
                    carried[col] = text
                else:
                    text = f"[merged] {carried[col]}" if carried.get(col) else ""
            else:
                carried.pop(col, None)
            cells.append(text)
            cells.extend([""] * (span - 1))  # a horizontal merge shows its value once
            col += span
        rows.append(cells)
    return rows


# ------------------------------------------------------------------ body


def _render_blocks(parent, styles, lines: list[str], counter: list[int]) -> None:
    for ch in parent:
        if ch.tag == q("p"):
            text = _paragraph_text(ch)
            if text:
                label = _heading_label(ch, styles)
                if label:
                    lines.append(f"[{label}] {text}")
                else:
                    lines.append((_list_prefix(ch) or "") + text)
            for box in _textboxes(ch):
                inner: list[str] = []
                _render_blocks(box, styles, inner, counter)
                lines.extend(f"[TEXT BOX] {l}" for l in inner)
        elif ch.tag == q("tbl"):
            counter[0] += 1
            lines.append(f"[TABLE {counter[0]}]")
            for n, cells in enumerate(_table_rows(ch, styles), 1):
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    lines.append(f"R{n}: " + " | ".join(cells))
        elif ch.tag in (q("sdt"), q("sdtContent")):
            sc = ch if ch.tag == q("sdtContent") else ch.find(q("sdtContent"))
            if sc is not None:
                _render_blocks(sc, styles, lines, counter)


def _part_lines(root, styles) -> list[str]:
    lines: list[str] = []
    body = root.find(q("body"))  # w:document wraps w:body; headers/footers/footnotes do not
    _render_blocks(body if body is not None else root, styles, lines, [0])
    return lines


def _load(z: zipfile.ZipFile, name: str):
    info = z.getinfo(name)
    if info.file_size > MAX_PART_BYTES:
        raise ValueError(f"{name} too large when unpacked ({info.file_size} bytes)")
    raw = z.read(name)
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in raw[:20000].lower():
        raise ValueError(f"{name} contains a DTD/entity declaration; refusing to parse")
    return ET.fromstring(raw)


def render_docx(data: bytes) -> RenderResult:
    try:
        return _render(data)
    except Exception as exc:
        return unreadable(f"unexpected error: {type(exc).__name__}: {exc}")


def _render(data: bytes) -> RenderResult:
    if not data:
        return unreadable("empty file (0 bytes)")
    if len(data) > MAX_BYTES:
        return unreadable(f"file too large ({len(data)} bytes, limit {MAX_BYTES})")
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return unreadable("encrypted or legacy (.doc) Word file; cannot open as .docx")
    if data[:2] != b"PK":
        return unreadable("not a .docx file (no zip signature)")
    warnings: list[str] = []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
        if "word/document.xml" not in names:
            return unreadable("zip has no word/document.xml; not a Word document")
        doc = _load(z, "word/document.xml")
    except zipfile.BadZipFile as exc:
        return unreadable(f"corrupt or truncated .docx (bad zip: {exc})")
    except ET.ParseError as exc:
        return unreadable(f"document.xml is not valid XML: {exc}")
    except Exception as exc:
        return unreadable(f"cannot open document: {type(exc).__name__}: {exc}")

    styles = {}
    if "word/styles.xml" in names:
        try:
            styles = _style_names(_load(z, "word/styles.xml"))
        except Exception as exc:
            warnings.append(f"styles.xml unreadable, headings may be missed: {type(exc).__name__}")

    def parts(prefix: str) -> list[tuple[str, list[str]]]:
        out, seen = [], set()
        for n in sorted(x for x in names if re.fullmatch(rf"word/{prefix}\d*\.xml", x)):
            try:
                ls = _part_lines(_load(z, n), styles)
            except Exception as exc:
                warnings.append(f"{n} unreadable: {type(exc).__name__}")
                out.append((n, [f"[UNREADABLE: {type(exc).__name__}]"]))
                continue
            key = tuple(ls)
            if ls and key not in seen:
                seen.add(key)
                out.append((n.split("/")[-1], ls))
        return out

    def section(kind: str, items) -> list[str]:
        lines = []
        for n, ls in items:
            lines.append(f"=== {kind} ({n}) ===")
            lines.extend(ls)
        return lines

    head = section("HEADER", parts("header"))
    foot = section("FOOTER", parts("footer")) + section("FOOTNOTES", parts("footnotes")) \
        + section("ENDNOTES", parts("endnotes"))

    body = _part_lines(doc, styles)
    out = [LEGEND] + head
    out.append("=== BODY ===")
    budget = max(1_000, MAX_TOTAL_CHARS - sum(len(l) + 1 for l in out + foot) - RESERVE_FOR_EDGES // 3)
    used = 0
    truncated = False
    for i, line in enumerate(body):
        if used + len(line) + 1 > budget:
            out.append(f"[TRUNCATED: document body cut; {len(body) - i} more lines not shown]")
            truncated = True
            break
        out.append(line)
        used += len(line) + 1
    out.extend(foot)
    if not body and not head and not foot:
        return unreadable("document has no text")
    text = "\n".join(out)
    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS] + f"\n[TRUNCATED: output cut at {MAX_TOTAL_CHARS} characters]"
        truncated = True
    return RenderResult(status="ok", text=text, truncated=truncated, warnings=warnings)
