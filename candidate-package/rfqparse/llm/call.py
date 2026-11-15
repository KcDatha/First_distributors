"""get_or_call: the one place a model call goes through the cache and the ledger.

The client is any object with `complete(payload) -> {"response": <json-able raw output>,
"usage": {"prompt_tokens": int, "completion_tokens": int}}`. This module has no network code.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .cache import Cache, CacheMiss, generation_settings, make_key
from .errors import LLMCallError, redact
from .ledger import Ledger, usd_for


@dataclass
class CallStats:
    real_calls: int = 0
    cache_hits: int = 0
    misses_without_client: int = 0
    cache_write_errors: int = 0
    failed_attempts: int = 0
    by_email: dict = field(default_factory=dict)   # email id -> {"real": n, "hits": n}

    def _bump(self, email_id: str, field_name: str) -> None:
        self.by_email.setdefault(email_id, {"real": 0, "hits": 0})[field_name] += 1


@dataclass
class CallResult:
    response: object
    usage: dict
    cache_hit: bool
    key: str
    latency_s: float
    usd: float | None


STATS = CallStats()   # default counters; pass your own `stats=` to keep a separate tally


def _label(err: Exception) -> str:
    """A short, key-free description of a failure for the ledger."""
    status = getattr(err, "status_code", None)
    return type(err).__name__ + (f" status={status}" if status else "")


def get_or_call(payload, client, *, model: str, prompt_version: str, schema_version: str, email_id: str,
                cache: Cache | None = None, ledger: Ledger | None = None, stats: CallStats | None = None) -> CallResult:
    """Return the cached response for this exact input, or call the client once and cache the answer.

    Raises CacheMiss when there is nothing cached and `client` is None (no key). Raises LLMCallError
    when the client fails. A cache or ledger that cannot be written never fails the call.
    """
    cache = cache or Cache()
    ledger = ledger or Ledger()
    stats = stats if stats is not None else STATS
    settings = generation_settings(model)   # temperature and output limit are part of the key
    key = make_key(model, prompt_version, schema_version, payload, settings)

    t0 = time.perf_counter()
    entry = cache.get(key)
    if entry is not None:
        lat = time.perf_counter() - t0
        pt, ct = int(entry.usage.get("prompt_tokens", 0)), int(entry.usage.get("completion_tokens", 0))
        ledger.record(email_id=email_id, model=model, prompt_tokens=pt, completion_tokens=ct, latency_s=lat,
                      cache_hit=True, prompt_version=prompt_version, key=key)
        stats.cache_hits += 1
        stats._bump(email_id, "hits")
        return CallResult(entry.response, entry.usage, True, key, lat, usd_for(model, pt, ct))

    if client is None:
        stats.misses_without_client += 1
        raise CacheMiss(key, model)

    def on_attempt(info: dict) -> None:   # a failed attempt inside the client's retry loop: one ledger line each
        stats.failed_attempts += 1
        err = info["error"]
        ledger.record(email_id=email_id, model=model, prompt_tokens=info.get("prompt_tokens", 0),
                      completion_tokens=info.get("completion_tokens", 0), latency_s=info["latency_s"], cache_hit=False,
                      prompt_version=prompt_version, key=key, status="error", error=_label(err))

    reports = bool(getattr(client, "reports_attempts", False))
    t1 = time.perf_counter()
    try:
        out = client.complete(payload, on_attempt=on_attempt) if reports else client.complete(payload)
    except CacheMiss:
        raise                                    # e.g. BudgetExceeded: the pipeline treats it as a miss
    except Exception as exc:
        if not reports:                          # clients that do not report attempts still leave a failure line
            on_attempt({"error": exc, "latency_s": time.perf_counter() - t1})
        if isinstance(exc, LLMCallError):
            raise
        raise LLMCallError(f"{type(exc).__name__}: {redact(exc)}") from exc
    lat = time.perf_counter() - t1
    try:
        response, usage = out["response"], dict(out["usage"])
        pt, ct = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        json.dumps(response, allow_nan=False)
    except Exception as exc:
        raise LLMCallError(f"client returned an unusable result ({type(exc).__name__}: {exc})") from exc

    try:
        cache.put(key, model=model, prompt_version=prompt_version, schema_version=schema_version, response=response,
                  usage=usage, latency_s=lat, email_id=email_id, generation=settings)
    except Exception:
        stats.cache_write_errors += 1   # still return the answer we paid for
    ledger.record(email_id=email_id, model=model, prompt_tokens=pt, completion_tokens=ct, latency_s=lat,
                  cache_hit=False, prompt_version=prompt_version, key=key)
    stats.real_calls += 1
    stats._bump(email_id, "real")
    return CallResult(response, usage, False, key, lat, usd_for(model, pt, ct))
