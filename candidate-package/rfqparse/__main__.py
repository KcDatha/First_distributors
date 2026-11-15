"""CLI: python -m rfqparse parse --emails <dir> --out parsed.json (stub)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_cmd(emails: Path, out: Path, only: list[str] | None) -> int:
    """The real pipeline. Missing key: cache only (uncached emails get a flagged safe entry)."""
    from . import pipeline
    from .llm.call import CallStats
    from .llm.cache import Cache
    from .llm.client import make_client
    from .llm.errors import MissingKey
    from .llm.ledger import Ledger

    try:
        client = make_client()
    except MissingKey:
        client = None
        print("OPENAI_API_KEY is not set: answering from the cache only; emails without a cached answer are flagged "
              "needs_review.", file=sys.stderr)
    stats = CallStats()
    result = pipeline.run_parse(emails, client, only=only, cache=Cache(), ledger=Ledger(), stats=stats)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pipeline.dumps(result), encoding="utf-8")
    spent = f"${client.guard.spent:.4f}" if client is not None else "n/a"
    print(f"{len(result)} emails written to {out}. model calls: {stats.real_calls} real, {stats.cache_hits} from cache, "
          f"{stats.failed_attempts} failed attempts; spent this run {spent}")
    return 0


def prompt_cmd(emails: Path | None, email_id: str | None) -> int:
    """Debug: print the system prompt, or (with --email) exactly what would be sent for one email."""
    from .llm.prompts import build_user_parts, system_prompt
    from .llm.routing import route_model
    from .ingest import ingest_eml
    from .render.registry import render_email

    if email_id is None:
        print(system_prompt())
        return 0
    mail = ingest_eml((emails or Path("data/dev/emails")) / f"{email_id}.eml")
    parts, sources = build_user_parts(render_email(mail), mail)
    print(f"routed to: {route_model(parts)}   allowed sources besides 'body': {sorted(sources)}\n")
    for p in parts:
        print(p["text"] if p["type"] == "text" else f"[IMAGE {p['mime']}, {len(p['data'])} bytes, detail {p['detail']}]")
    return 0


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
    pr = sub.add_parser("prompt", help="debug: print the system prompt, or what would be sent for one email")
    pr.add_argument("--emails", type=Path, default=None)
    pr.add_argument("--email", default=None, help="an email id, e.g. E001")
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
    p.add_argument("--only", default=None, help="comma-separated email ids to process (output holds just those)")
    args = ap.parse_args(argv)
    if args.cmd == "render":
        return render_dir(args.emails, args.out)
    if args.cmd == "prompt":
        return prompt_cmd(args.emails, args.email)
    if args.cmd == "cost":
        use_ledger = args.ledger is not None
        return cost_cmd(args.cache_dir, None if args.ledger is True else args.ledger, use_ledger)

    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    return parse_cmd(args.emails, args.out, only)


if __name__ == "__main__":
    sys.exit(main())
