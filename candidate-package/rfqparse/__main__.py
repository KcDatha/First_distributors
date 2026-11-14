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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rfqparse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="parse a directory of .eml files")
    p.add_argument("--emails", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    result = parse_dir(args.emails)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
