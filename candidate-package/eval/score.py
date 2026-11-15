#!/usr/bin/env python3
"""Score a parsed.json against an answer key, mirroring "What we grade" in data/README.md.

    python eval/score.py parsed.json --key data/dev/answer_key.json [--per-email]

Stdlib only. Section order: silent losses, hallucinated lines, lost/phantom requests,
classification, line recall/precision, paired-line fields, link/review fields.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PAIR_THRESHOLD = 0.5  # min Jaccard overlap of raw tokens for two lines to be paired

REQUEST_CLASSES = {"rfq", "rfq_revision", "purchase_order"}

# Spelling equivalences from data/README.md; applied to BOTH sides before comparing.
# All patterns are whole-word (\b), so RED/MI/CS never match inside another word.
_EQUIV = [
    (r"\bschedule\b", "sch"),
    (r"\bsch\s*(\d)", r"sch \1"),
    (r"\bclass\s*(\d+)", r"\1lb"),
    (r"(\d+)\s*(?:#|lbs?\b)", r"\1lb"),
    (r"\bbutt[\s-]*weld(?:ed)?\b", "bw"),
    (r"\b(?:threaded|thrd|thd)\b", "npt"),
    (r"\bseamless\b", "smls"),
    (r"\bgalvanized\b", "galv"),
    (r"\bells?\b", "elbow"),
    (r"\bvlvs?\b", "valve"),
    (r"\bflgs?\b", "flange"),
    (r"\bgskts?\b", "gasket"),
    (r"\bspw\b", "spiral wound"),
    (r"\bmi\b", "malleable iron"),
    (r"\bcs\b", "carbon steel"),
    (r"\bfp\b", "full port"),
    (r"\bred\b", "reducer"),
    (r"\bcplgs?\b", "coupling"),
]


def norm_tokens(text: str) -> list[str]:
    """Lowercase, apply spelling equivalences, split into alphanumeric tokens."""
    t = (text or "").lower()
    for pat, rep in _EQUIV:
        t = re.sub(pat, rep, t)
    return re.findall(r"[a-z0-9]+", t)


def phrase_in(phrase: str, desc_tokens: list[str]) -> bool:
    """Whole-token match, spaces ignored: the phrase's tokens, glued together, must equal
    some run of consecutive description tokens glued together (so `sch 40` == `sch40`,
    but `150` never matches inside `1500`). `a|b` means either; an empty phrase never matches."""
    for alt in phrase.split("|"):
        target = "".join(norm_tokens(alt))
        if not target:
            continue
        for i in range(len(desc_tokens)):
            acc = ""
            for tok in desc_tokens[i:]:
                acc += tok
                if acc == target:
                    return True
                if len(acc) >= len(target):
                    break
    return False


def tokens(raw: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (raw or "").lower()))


def overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def pair_lines(key_lines: list[dict], our_lines: list[dict]) -> list[tuple[int, int]]:
    """One-to-one greedy pairing on raw-token overlap, best score first."""
    kt = [tokens(l.get("raw")) for l in key_lines]
    ot = [tokens(l.get("raw")) for l in our_lines]
    cands = []
    for i, a in enumerate(kt):
        for j, b in enumerate(ot):
            s = overlap(a, b)
            if s >= PAIR_THRESHOLD:
                cands.append((-s, i, j))
    cands.sort()
    used_k, used_o, pairs = set(), set(), []
    for _, i, j in cands:
        if i not in used_k and j not in used_o:
            used_k.add(i)
            used_o.add(j)
            pairs.append((i, j))
    return sorted(pairs)


def uom_norm(u) -> str:
    return (u or "EA").upper()


def qty_ok(key_line: dict, ours: dict) -> bool | None:
    tol = key_line.get("qty_tol")
    if tol is None:
        return None  # unscored
    kq, oq = key_line.get("qty"), ours.get("qty")
    if kq is None:
        return oq is None
    if not isinstance(oq, (int, float)) or isinstance(oq, bool):
        return False
    return abs(oq - kq) <= tol * abs(kq)


def context_ok(key_line: dict, ours: dict) -> bool:
    d = norm_tokens(ours.get("description") or "")
    if not all(phrase_in(p, d) for p in key_line.get("must_mention") or []):
        return False
    return not any(phrase_in(p, d) for p in key_line.get("must_not_mention") or [])


def ratio(num: int, den: int) -> str:
    return f"{num}/{den} = {num / den:.1%}" if den else f"{num}/{den} = n/a"


def load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("emails"), dict):
        data = data["emails"]
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object keyed by email id")
    return data


def lines_of(entry) -> list[dict]:
    ls = entry.get("line_items") if isinstance(entry, dict) else None
    return [l for l in ls if isinstance(l, dict)] if isinstance(ls, list) else []


def get(entry, field):
    return entry.get(field) if isinstance(entry, dict) else None


def short(s, n=90) -> str:
    s = re.sub(r"\s+", " ", str(s))
    return s if len(s) <= n else s[: n - 1] + "…"


def compute(key: dict, out: dict) -> dict:
    """All metrics as plain data. `qty`, `uom`, `src`, `ctx`, `ctx_c` are [correct, scored]."""
    ids = sorted(key)
    m = {
        "ids": ids,
        "missing": [i for i in ids if i not in out],
        "extra": sorted(set(out) - set(key)),
        "silent": [], "halluc": [], "lost": [], "phantom": [], "cls_wrong": [], "missed": [],
        "n_key": 0, "n_our": 0, "n_paired": 0,
        "qty": [0, 0], "uom": [0, 0], "src": [0, 0], "ctx": [0, 0], "ctx_c": [0, 0],
        "nr": {"tp": 0, "fp": 0, "fn": 0},
        "link": {f: {"tp": 0, "ours": 0, "key": 0} for f in ("revises", "duplicate_of")},
        "per_email": [],
    }
    for i in ids:
        k, o = key[i], out.get(i)
        kl, ol = lines_of(k), lines_of(o)
        pairs = pair_lines(kl, ol)
        pk = {p[0] for p in pairs}
        po = {p[1] for p in pairs}
        m["n_key"] += len(kl)
        m["n_our"] += len(ol)
        m["n_paired"] += len(pairs)

        needs_action = bool(kl) or bool(k.get("needs_review"))
        our_review = bool(get(o, "needs_review"))
        if needs_action and not ol and not our_review:
            m["silent"].append(i)
        m["halluc"] += [(i, l.get("raw")) for j, l in enumerate(ol) if j not in po]
        m["missed"] += [(i, l.get("raw")) for n, l in enumerate(kl) if n not in pk]

        kc, oc = k.get("classification"), get(o, "classification")
        if kc != oc:
            m["cls_wrong"].append((i, kc, oc))
        if kc in REQUEST_CLASSES and oc not in REQUEST_CLASSES:
            m["lost"].append(i)
        if kc not in REQUEST_CLASSES and oc in REQUEST_CLASSES:
            m["phantom"].append(i)

        row = {"id": i, "pairs": len(pairs), "qty": 0, "uom": 0, "src": 0, "ctx": 0}
        for n, j in pairs:
            kline, oline = kl[n], ol[j]
            q = qty_ok(kline, oline)
            if q is not None:
                m["qty"][1] += 1
                m["qty"][0] += q
                row["qty"] += q
            for name, ok in (
                ("uom", uom_norm(kline.get("uom")) == uom_norm(oline.get("uom"))),
                ("src", kline.get("source") == oline.get("source")),
                ("ctx", context_ok(kline, oline)),
            ):
                m[name][1] += 1
                m[name][0] += ok
                row[name] += ok
                if name == "ctx" and (kline.get("must_mention") or kline.get("must_not_mention")):
                    m["ctx_c"][1] += 1
                    m["ctx_c"][0] += ok
        m["per_email"].append((row, kc, oc, len(kl), len(ol), get(o, "needs_review"), k.get("needs_review")))

        kn = bool(k.get("needs_review"))
        m["nr"]["tp"] += kn and our_review
        m["nr"]["fp"] += (not kn) and our_review
        m["nr"]["fn"] += kn and not our_review
        for f, v in m["link"].items():
            kv, ov = k.get(f), get(o, f)
            v["key"] += kv is not None
            v["ours"] += ov is not None
            v["tp"] += kv is not None and kv == ov
    return m


def report(m: dict, per_email: bool) -> None:
    n = len(m["ids"])
    print(f"emails in key: {n}   missing from output: {len(m['missing'])} {m['missing'] or ''}"
          f"   extra in output: {len(m['extra'])} {m['extra'] or ''}")
    print(f"pairing threshold (token Jaccard on raw): {PAIR_THRESHOLD}\n")

    print(f"1. SILENT LOSSES: {len(m['silent'])}")
    print("   ids:", ", ".join(m["silent"]) or "none")

    print(f"\n2. HALLUCINATED LINES (ours, no pair in key): {len(m['halluc'])}")
    for i, raw in m["halluc"]:
        print(f"   {i}  {short(raw)}")

    print(f"\n3. LOST REQUESTS: {len(m['lost'])}   ids: {', '.join(m['lost']) or 'none'}")
    print(f"   PHANTOM REQUESTS: {len(m['phantom'])}   ids: {', '.join(m['phantom']) or 'none'}")

    print(f"\n4. CLASSIFICATION ACCURACY: {ratio(n - len(m['cls_wrong']), n)}")
    for i, kc, oc in m["cls_wrong"]:
        print(f"   {i}  key={kc}  ours={oc}")

    print(f"\n5. LINES  recall: {ratio(m['n_paired'], m['n_key'])}   precision: {ratio(m['n_paired'], m['n_our'])}")

    print(f"\n6. PAIRED LINES ({m['n_paired']})")
    print(f"   qty (qty_tol):        {ratio(*m['qty'])}")
    print(f"   uom (null = EA):      {ratio(*m['uom'])}")
    print(f"   source:               {ratio(*m['src'])}")
    print(f"   context carried:      {ratio(*m['ctx'])}   (only lines with constraints: {ratio(*m['ctx_c'])})")

    print("\n7. LINKS / REVIEW")
    for f, v in m["link"].items():
        print(f"   {f}: precision {ratio(v['tp'], v['ours'])}   recall {ratio(v['tp'], v['key'])}")
    nr = m["nr"]
    print(f"   needs_review: precision {ratio(nr['tp'], nr['tp'] + nr['fp'])}"
          f"   recall {ratio(nr['tp'], nr['tp'] + nr['fn'])}")

    print(f"\nUNPAIRED KEY LINES (missed): {len(m['missed'])}")
    for i, raw in m["missed"]:
        print(f"   {i}  {short(raw)}")
    print(f"UNPAIRED OUR LINES: {len(m['halluc'])}  (listed in section 2)")

    if per_email:
        print("\nPER EMAIL")
        print("   id    class(key -> ours)               lines key/ours/paired   qty uom src ctx   review key/ours")
        for row, kc, oc, nk, no, orv, krv in m["per_email"]:
            print(f"   {row['id']}  {str(kc) + ' -> ' + str(oc):<32} {nk:>3}/{no:>3}/{row['pairs']:>3}"
                  f"            {row['qty']:>3} {row['uom']:>3} {row['src']:>3} {row['ctx']:>3}"
                  f"   {bool(krv)!s:<5}/{bool(orv)}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("parsed", type=Path)
    ap.add_argument("--key", type=Path, required=True)
    ap.add_argument("--per-email", action="store_true", help="add a per-email breakdown")
    ap.add_argument("--only", default=None, help="comma-separated email ids: score only those")
    a = ap.parse_args(argv)
    try:
        out, key = load(a.parsed), load(a.key)
    except Exception as exc:
        print(f"cannot load inputs: {exc}", file=sys.stderr)
        return 1
    if a.only:
        ids = {x.strip() for x in a.only.split(",") if x.strip()}
        unknown = sorted(ids - set(key))
        if unknown:
            print(f"--only names ids not in the key: {unknown}", file=sys.stderr)
            return 1
        key = {k: v for k, v in key.items() if k in ids}
        out = {k: v for k, v in out.items() if k in ids}
        print(f"(scoring only {', '.join(sorted(ids))})")
    report(compute(key, out), a.per_email)
    return 0


if __name__ == "__main__":
    sys.exit(main())
