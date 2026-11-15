"""get_or_call: the one place a model call goes through the cache and the ledger.

The client is any object with `complete(payload) -> {"response": <json-able raw output>,
"usage": {"prompt_tokens": int, "completion_tokens": int}}`. This module has no network code.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .cache import Cache, CacheMiss, make_key
from .ledger import Ledger, usd_for


class LLMCallError(Exception):
    """The client failed or returned something unusable. Nothing is cached for a failed call."""


@dataclass
class CallStats:
    real_calls: int = 0
    cache_hits: int = 0
    misses_without_client: int = 0
    cache_write_errors: int = 0
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


def get_or_call(payload, client, *, model: str, prompt_version: str, schema_version: str, email_id: str,
                cache: Cache | None = None, ledger: Ledger | None = None, stats: CallStats | None = None) -> CallResult:
    """Return the cached response for this exact input, or call the client once and cache the answer.

    Raises CacheMiss when there is nothing cached and `client` is None (no key). Raises LLMCallError
    when the client fails. A cache or ledger that cannot be written never fails the call.
    """
    cache = cache or Cache()
    ledger = ledger or Ledger()
    stats = stats if stats is not None else STATS
    key = make_key(model, prompt_version, schema_version, payload)

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

    t1 = time.perf_counter()
    try:
        out = client.complete(payload)
    except Exception as exc:
        raise LLMCallError(f"{type(exc).__name__}: {exc}") from exc
    lat = time.perf_counter() - t1
    try:
        response, usage = out["response"], dict(out["usage"])
        pt, ct = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        json.dumps(response, allow_nan=False)
    except Exception as exc:
        raise LLMCallError(f"client returned an unusable result ({type(exc).__name__}: {exc})") from exc

    try:
        cache.put(key, model=model, prompt_version=prompt_version, schema_version=schema_version, response=response,
                  usage=usage, latency_s=lat, email_id=email_id)
    except Exception:
        stats.cache_write_errors += 1   # still return the answer we paid for
    ledger.record(email_id=email_id, model=model, prompt_tokens=pt, completion_tokens=ct, latency_s=lat,
                  cache_hit=False, prompt_version=prompt_version, key=key)
    stats.real_calls += 1
    stats._bump(email_id, "real")
    return CallResult(response, usage, False, key, lat, usd_for(model, pt, ct))
