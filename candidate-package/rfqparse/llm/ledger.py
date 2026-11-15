"""Cost ledger (one JSON line per lookup) and the cost report. Stdlib only."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

from .. import config
from .cache import Cache


def default_ledger_path() -> Path:
    return Path(__file__).resolve().parents[2] / "cost_ledger.jsonl"


def price_for(model: str) -> dict | None:
    return config.MODEL_PRICES.get(model)


def usd_for(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Dollars for these tokens, or None when the model has no price in config.MODEL_PRICES."""
    p = price_for(model)
    if p is None:
        return None
    return round((prompt_tokens * p["input_per_1m"] + completion_tokens * p["output_per_1m"]) / 1_000_000, 6)


class Ledger:
    def __init__(self, path: Path | str | None = None):
        chosen = path if path is not None else config.LEDGER_PATH
        self.path = Path(chosen) if chosen is not None else default_ledger_path()
        self.write_errors = 0

    def record(self, *, email_id: str, model: str, prompt_tokens: int, completion_tokens: int, latency_s: float,
               cache_hit: bool, prompt_version: str = "", key: str = "", status: str = "ok",
               error: str | None = None) -> dict:
        """Append one line. Never raises: a ledger that cannot be written must not stop the run.

        status "error" lines (failed attempts) have usd null. `error` is a short label only (class and HTTP
        status), never a provider message.
        """
        row = {"email_id": email_id, "model": model, "prompt_version": prompt_version, "key": key[:16],
               "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
               "latency_s": round(latency_s, 3),
               "usd": None if status == "error" else usd_for(model, prompt_tokens, completion_tokens),
               "cache_hit": bool(cache_hit), "status": status, "error": error}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        except Exception:
            self.write_errors += 1
        return row

    def rows(self) -> tuple[list[dict], int]:
        """All readable ledger lines, plus how many lines were unreadable."""
        rows, bad = [], 0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return rows, bad
        for ln in lines:
            if not ln.strip():
                continue
            try:
                d = json.loads(ln)
                if not isinstance(d, dict):
                    raise ValueError
                rows.append(d)
            except Exception:
                bad += 1
        return rows, bad


# ------------------------------------------------------------------ the report


def _fmt_usd(v) -> str:
    return "unknown" if v is None else f"${v:,.4f}"


def _table(groups: "OrderedDict[str, dict]", cols: list[tuple[str, str]], total: dict, missing_price: set[str]) -> list[str]:
    head = f"{'email':<10}" + "".join(f"{label:>14}" for _, label in cols)
    lines = [head, "-" * len(head)]
    for eid, g in groups.items():
        lines.append(f"{eid:<10}" + "".join(f"{(_fmt_usd(g[k]) if k.startswith('usd') else g[k]):>14}" for k, _ in cols))
    lines.append("-" * len(head))
    lines.append(f"{'TOTAL':<10}" + "".join(f"{(_fmt_usd(total[k]) if k.startswith('usd') else total[k]):>14}" for k, _ in cols))
    return lines


def _banners(models: set[str]) -> list[str]:
    out = []
    if config.PRICES_ARE_PLACEHOLDER:
        out.append("!! PRICES ARE PLACEHOLDERS (config.MODEL_PRICES): dollar figures are not real until the model is chosen")
    missing = sorted(m for m in models if price_for(m) is None)
    if missing:
        out.append("!! no price for model(s): " + ", ".join(missing) + " -> dollars shown as 'unknown' for those calls; "
                   "add them to config.MODEL_PRICES")
    return out


def _add(g: dict, pt: int, ct: int, usd, lat) -> None:
    g["tokens_in"] += pt
    g["tokens_out"] += ct
    g["latency"] = round(g["latency"] + (lat or 0), 3)
    if usd is None:
        g["unpriced"] = True
    elif g["usd"] is not None:
        g["usd"] = round(g["usd"] + usd, 6)


def _new() -> dict:
    return {"calls": 0, "real": 0, "hits": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0, "usd_spent": 0.0,
            "latency": 0.0, "unpriced": False, "errors": 0}


def _finish(groups: dict) -> None:
    for g in groups.values():
        if g.pop("unpriced"):
            g["usd"] = None


def cost_from_cache(cache: Cache) -> str:
    """Cost of producing the cached responses (one entry = one model call), per email. Needs no key and no ledger."""
    entries, bad = cache.entries()
    groups: "OrderedDict[str, dict]" = OrderedDict()
    models: set[str] = set()
    for e in sorted(entries, key=lambda x: (x.email_id or "~unattributed", x.key)):
        g = groups.setdefault(e.email_id or "~unattributed", _new())
        g["calls"] += 1
        models.add(e.model)
        pt, ct = int(e.usage.get("prompt_tokens", 0)), int(e.usage.get("completion_tokens", 0))
        _add(g, pt, ct, usd_for(e.model, pt, ct), e.latency_s)
    _finish(groups)
    total = _new()
    for g in groups.values():
        for k in ("calls", "tokens_in", "tokens_out"):
            total[k] += g[k]
        total["usd"] = None if (total["usd"] is None or g["usd"] is None) else round(total["usd"] + g["usd"], 6)
    cols = [("calls", "model calls"), ("tokens_in", "prompt tok"), ("tokens_out", "compl. tok"), ("usd", "usd")]
    lines = [f"COST FROM CACHE ENTRIES in {cache.dir}", f"{len(entries)} entries ({bad} unreadable, ignored)"]
    lines += _banners(models)
    if not groups:
        return "\n".join(lines + ["(no cache entries)"])
    lines += _table(groups, cols, total, set())
    return "\n".join(lines)


def cost_from_ledger(ledger: Ledger) -> str:
    """Every lookup in the ledger: real calls vs cache hits, tokens, dollars (spent = real calls only)."""
    rows, bad = ledger.rows()
    groups: "OrderedDict[str, dict]" = OrderedDict()
    models: set[str] = set()
    for r in sorted(rows, key=lambda r: str(r.get("email_id"))):  # stable regardless of file order
        g = groups.setdefault(str(r.get("email_id")), _new())
        if r.get("status") == "error":     # a failed attempt: counted, but it has no priced result
            g["errors"] += 1
            models.add(str(r.get("model")))
            continue
        hit = bool(r.get("cache_hit"))
        g["calls"] += 1
        g["hits" if hit else "real"] += 1
        models.add(str(r.get("model")))
        pt, ct = int(r.get("prompt_tokens") or 0), int(r.get("completion_tokens") or 0)
        usd = r.get("usd")
        _add(g, pt, ct, usd, r.get("latency_s"))
        if not hit and usd is not None:
            g["usd_spent"] = round(g["usd_spent"] + usd, 6)
    _finish(groups)
    total = _new()
    for g in groups.values():
        for k in ("calls", "real", "hits", "tokens_in", "tokens_out", "errors"):
            total[k] += g[k]
        total["usd"] = None if (total["usd"] is None or g["usd"] is None) else round(total["usd"] + g["usd"], 6)
        total["usd_spent"] = round(total["usd_spent"] + g["usd_spent"], 6)
    cols = [("calls", "lookups"), ("real", "real calls"), ("hits", "cache hits"), ("tokens_in", "prompt tok"),
            ("tokens_out", "compl. tok"), ("errors", "failed tries"), ("usd", "usd (priced)"), ("usd_spent", "usd spent")]
    lines = [f"COST FROM LEDGER {ledger.path}", f"{len(rows)} lookups ({bad} unreadable lines, ignored)",
             "usd (priced) = what the tokens cost at table prices; usd spent = only the real calls"]
    lines += _banners(models)
    if not groups:
        return "\n".join(lines + ["(ledger is empty)"])
    lines += _table(groups, cols, total, set())
    return "\n".join(lines)
