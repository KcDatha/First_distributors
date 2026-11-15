"""CLI: python -m rfqparse parse --emails <dir> --out parsed.json (stub)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _stub_entry() -> dict:
    return {
        "classification": "rfq",
        "revises": None,
        "duplicate_of": None,
        "needs_review": True,
        "review_reason": "stub: not parsed",
        "line_items": [],
    }


def parse_dir(emails: Path) -> dict:
    result: dict = {}
    for path in sorted(emails.glob("*.eml")):
        try:
            path.read_bytes()  # real parsing goes here later
            result[path.stem] = _stub_entry()
        except Exception as exc:  # one bad email must never stop the run
            entry = _stub_entry()
            entry["review_reason"] = f"stub: unreadable email ({type(exc).__name__})"
            result[path.stem] = entry
    return result


def render_dir(emails: Path, out: Path) -> int:
    """Debug: write one readable text file per email with everything the model would see."""
    from .render.registry import format_package, render_path, summary_line

    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(emails.glob("*.eml")):
        pkg = render_path(path)
        try:
            (out / f"{path.stem}.txt").write_text(format_package(pkg), encoding="utf-8")
            print(summary_line(pkg))
        except Exception as exc:  # one bad email must not stop the run
            print(f"{path.stem} | FAILED to write: {type(exc).__name__}: {exc}")
    return 0


def cost_cmd(cache_dir: Path | None, ledger: Path | None, use_ledger: bool) -> int:
    from .llm.cache import Cache
    from .llm.ledger import Ledger, cost_from_cache, cost_from_ledger

    print(cost_from_ledger(Ledger(ledger)) if use_ledger else cost_from_cache(Cache(cache_dir)))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rfqparse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cost", help="per-email and total model calls, tokens and dollars (no key needed)")
    c.add_argument("--cache-dir", type=Path, default=None, help="cache folder (default: cache/ at the repo root)")
    c.add_argument("--ledger", nargs="?", const=True, default=None, type=Path,
                   help="read cost_ledger.jsonl instead of the cache entries (optionally give its path)")
    r = sub.add_parser("render", help="debug: write what the model would see, one text file per email")
    r.add_argument("--emails", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("parse", help="parse a directory of .eml files")
    p.add_argument("--emails", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "render":
        return render_dir(args.emails, args.out)
    if args.cmd == "cost":
        use_ledger = args.ledger is not None
        return cost_cmd(args.cache_dir, None if args.ledger is True else args.ledger, use_ledger)

    result = parse_dir(args.emails)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
