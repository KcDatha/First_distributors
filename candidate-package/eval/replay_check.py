"""Replay check: does the committed cache reproduce the output with no key and no model calls?

    python eval/replay_check.py [--emails data/dev/emails]

Runs `python -m rfqparse parse` twice with OPENAI_API_KEY removed, into temp files (and a temp ledger, so the repo's
cost_ledger.jsonl is untouched), from a different working directory. PASS only if both runs exit 0, report 0 real
model calls and 0 failed attempts, every email was answered from the cache, and the two outputs are byte-identical.
Exit 0 on PASS, 1 on FAIL. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLS = re.compile(r"model calls: (\d+) real, (\d+) from cache, (\d+) failed attempts")


def run_once(emails: Path, out: Path, ledger: Path, cwd: Path) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    env["RFQPARSE_LEDGER"] = str(ledger)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "-m", "rfqparse", "parse", "--emails", str(emails), "--out", str(out)],
                       cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check(emails: Path) -> list[str]:
    """The list of reasons for failing (empty = pass)."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        outs, cached_misses = [], []
        for n in (1, 2):
            out = tmp / f"run{n}" / "parsed.json"
            code, text = run_once(emails, out, tmp / "ledger.jsonl", tmp)
            m = CALLS.search(text)
            if code != 0:
                problems.append(f"run {n} exited {code}: {text.strip()[-300:]}")
            if not m:
                problems.append(f"run {n} did not report its model-call counts")
            else:
                real, hits, failed = map(int, m.groups())
                if real or failed:
                    problems.append(f"run {n} made {real} real call(s) and {failed} failed attempt(s); expected 0")
            if not out.exists():
                problems.append(f"run {n} wrote no output file")
                continue
            outs.append(out.read_bytes())
            try:
                data = json.loads(outs[-1].decode("utf-8"))
                miss = [k for k, v in data.items() if "no cached model answer" in (v.get("review_reason") or "")]
                if miss:
                    cached_misses.append(f"run {n}: no cached answer for {len(miss)} email(s): {', '.join(sorted(miss)[:8])}")
            except Exception as exc:
                problems.append(f"run {n} output is not valid JSON: {exc}")
        problems += cached_misses[:1]
        if len(outs) == 2 and outs[0] != outs[1]:
            problems.append("the two outputs are not byte-identical")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--emails", type=Path, default=ROOT / "data" / "dev" / "emails")
    a = ap.parse_args(argv)
    if not a.emails.is_dir():
        print(f"FAIL: emails folder not found: {a.emails}")
        return 1
    problems = check(a.emails)
    if problems:
        print("FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("PASS: two keyless runs exited 0, made 0 model calls, answered every email from the cache, and wrote identical bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
