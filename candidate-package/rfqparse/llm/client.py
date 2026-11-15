"""OpenAI client with structured outputs, our own retries, a spend guard, and typed errors.

The SDK's built-in retries are off (max_retries=0) so every attempt goes through here and into the
ledger. The API key is read from OPENAI_API_KEY, held only inside the SDK object, and never printed,
logged, cached or written to the ledger.
"""

from __future__ import annotations

import base64
import json
import os
import random
import time
from dataclasses import dataclass

import openai

from .. import config
from .errors import (ApiFailure, BudgetExceeded, MissingKey, ModelNotPriced, ModelRefusal, OutputTruncated,
                     SchemaMismatch, redact)
from .ledger import price_for, usd_for
from .schema import check_strict, validate


@dataclass
class ModelOutput:
    parsed: object            # the JSON the model returned, checked against the schema
    raw_text: str
    finish_reason: str
    usage: dict               # {"prompt_tokens": int, "completion_tokens": int}
    latency_s: float          # of the successful request only
    attempts: int             # requests made, including failed ones
    refusal: str | None = None


class SpendGuard:
    """Adds up what real calls cost in this run and refuses further real calls past the limit."""

    def __init__(self, limit_usd: float | None = None):
        self.limit = float(config.MAX_USD_PER_RUN if limit_usd is None else limit_usd)
        self.spent = 0.0

    def check(self, model: str = "") -> None:
        if self.spent >= self.limit:
            raise BudgetExceeded(self.spent, self.limit, model)

    def add(self, usd: float | None) -> None:
        if usd:
            self.spent += usd


def _try_json(text: str):
    try:
        return json.loads(text) if text.strip() else None
    except Exception:
        return None


def check_reply(response: dict, schema: dict, extra_validator=None) -> tuple[object | None, Exception | None]:
    """Judge a model reply (the dict get_or_call returns/caches). Returns (parsed, None) when it is good, else
    (None, a typed error: ModelRefusal, OutputTruncated or SchemaMismatch). This runs OUTSIDE the cache layer,
    so a bad reply can be cached and a replay with no key reaches the same verdict.

    extra_validator(parsed) may return a list of problem strings for checks the schema cannot express.
    """
    refusal = response.get("refusal")
    finish = response.get("finish_reason") or ""
    if refusal or finish == "content_filter":
        return None, ModelRefusal(f"model refused ({'content filter' if finish == 'content_filter' and not refusal else 'refusal'})",
                                  refusal=redact(refusal or ""))
    if finish == "length":
        return None, OutputTruncated("output cut off at the length limit")
    parsed, text = response.get("parsed"), response.get("raw_text") or ""
    if parsed is None:
        return None, SchemaMismatch(f"reply is not valid JSON: {redact(text, 120)!r}" if text.strip() else "reply was empty")
    problems = validate(parsed, schema)
    if problems:
        return None, SchemaMismatch("reply does not match the schema: " + "; ".join(problems[:5]))
    if extra_validator is not None:
        extra = extra_validator(parsed) or []
        if extra:
            return None, SchemaMismatch("reply failed validation: " + "; ".join(str(x) for x in extra[:5]))
    return parsed, None


def settings_for(model: str) -> dict:
    return {**config.DEFAULT_MODEL_SETTINGS, **config.MODEL_SETTINGS.get(model, {})}


def build_payload(*, model: str, system: str, user: list[dict], schema: dict, schema_name: str = "rfq_output") -> dict:
    """The one dict that describes a call; it is also what the cache key is made from.

    `user` is a list of parts: {"type": "text", "text": ...} or
    {"type": "image", "data": <bytes>, "mime": "image/png", "detail": "high"}.
    """
    return {"model": model, "system": system, "user": user, "schema": schema, "schema_name": schema_name}


def _messages(system: str, user: list[dict]) -> list[dict]:
    parts = []
    for p in user:
        if p.get("type") == "text":
            parts.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image":
            url = f"data:{p.get('mime', 'image/png')};base64,{base64.b64encode(bytes(p['data'])).decode('ascii')}"
            parts.append({"type": "image_url", "image_url": {"url": url, "detail": p.get("detail", config.LLM_IMAGE_DETAIL)}})
        else:
            raise ValueError(f"unknown user content part type: {p.get('type')!r}")
    return [{"role": "system", "content": system}, {"role": "user", "content": parts}]


def _classify(exc: Exception) -> tuple[str, bool, int | None]:
    """(kind, retryable, http status) for an SDK exception."""
    if isinstance(exc, openai.APITimeoutError):
        return "timeout", True, None
    if isinstance(exc, openai.APIConnectionError):
        return "connection", True, None
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if code == 429:
            quota = getattr(exc, "code", None) == "insufficient_quota"
            return ("quota" if quota else "rate_limit"), (not quota), code   # out of quota will not fix itself
        if code == 408:
            return "timeout", True, code
        if code >= 500:
            return "server", True, code
        if code in (401, 403):
            return "auth", False, code
        return "client_error", False, code        # 400 and the rest: our request is wrong, retrying cannot help
    return "unknown", False, None


def _retry_after(exc: Exception) -> float | None:
    try:
        v = exc.response.headers.get("retry-after")  # type: ignore[attr-defined]
        return float(v) if v is not None else None
    except Exception:
        return None


def call_model(sdk, *, system: str, user: list[dict], schema: dict, model: str, schema_name: str = "rfq_output",
               on_attempt=None, sleep=time.sleep, rng=random.random, guard: SpendGuard | None = None,
               strict_reply: bool = True) -> ModelOutput:
    """One structured-output call with retries. Returns parsed JSON, usage and latency; raises only typed errors.

    on_attempt(info) is called for every FAILED attempt (the caller writes a ledger line for each).
    strict_reply=True raises ModelRefusal / OutputTruncated / SchemaMismatch for a bad reply. With False
    (what OpenAIClient uses) a reply that was received and billed is returned as is, even when unusable,
    so it can be cached; judge it afterwards with check_reply(), outside the cache layer.
    """
    check_strict(schema)
    if price_for(model) is None:
        raise ModelNotPriced(f"no price for model {model!r} in config.MODEL_PRICES; the spend guard cannot count it")
    st = settings_for(model)
    kwargs = {"model": model, "messages": _messages(system, user),
              "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
              "max_completion_tokens": st["max_completion_tokens"],
              "timeout": openai.Timeout(config.LLM_TIMEOUT_S, connect=config.LLM_CONNECT_TIMEOUT_S)}
    if st.get("temperature") is not None:
        kwargs["temperature"] = st["temperature"]

    def failed(err: Exception, t0: float, attempt: int, will_retry: bool, usage: dict | None = None) -> None:
        if on_attempt:
            try:
                on_attempt({"error": err, "latency_s": time.perf_counter() - t0, "attempt": attempt, "will_retry": will_retry,
                            "prompt_tokens": (usage or {}).get("prompt_tokens", 0),
                            "completion_tokens": (usage or {}).get("completion_tokens", 0)})
            except Exception:
                pass   # a broken ledger hook must not change the call's outcome

    max_attempts = max(1, int(config.LLM_MAX_ATTEMPTS))
    for attempt in range(1, max_attempts + 1):
        if guard:
            guard.check(model)
        t0 = time.perf_counter()
        try:
            resp = sdk.chat.completions.create(**kwargs)
        except Exception as exc:
            kind, retryable, status = _classify(exc)
            will_retry = retryable and attempt < max_attempts
            msg = f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "") + \
                  ("" if kind == "auth" else f": {redact(getattr(exc, 'message', '') or str(exc))}")
            err = ApiFailure(f"{kind}: {msg}", kind=kind, status_code=status, attempts=attempt)
            failed(err, t0, attempt, will_retry)
            if not will_retry:
                raise err from None
            cap = min(config.LLM_BACKOFF_MAX_S, config.LLM_BACKOFF_BASE_S * 2 ** (attempt - 1))
            delay = cap / 2 + rng() * cap / 2                       # exponential, with jitter
            ra = _retry_after(exc)
            if ra is not None:
                delay = max(delay, min(ra, config.LLM_BACKOFF_MAX_S))
            sleep(delay)
            continue
        latency = time.perf_counter() - t0
        u = getattr(resp, "usage", None)
        usage = {"prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0), "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0)}
        if guard:
            guard.add(usd_for(model, usage["prompt_tokens"], usage["completion_tokens"]))   # a reply was billed even if unusable
        try:
            choice = resp.choices[0]
            msg_obj = choice.message
            finish = choice.finish_reason or ""
        except Exception:
            err = SchemaMismatch("response had no choices")
            failed(err, t0, attempt, False, usage)
            raise err from None
        refusal = getattr(msg_obj, "refusal", None)
        text = getattr(msg_obj, "content", None) or ""
        if not strict_reply:
            try:
                parsed = json.loads(text) if text.strip() else None
            except Exception:
                parsed = None
            return ModelOutput(parsed=parsed, raw_text=text, finish_reason=finish, usage=usage, latency_s=latency,
                               attempts=attempt, refusal=redact(refusal) if refusal else None)
        problem = check_reply({"parsed": _try_json(text), "raw_text": text, "finish_reason": finish,
                               "refusal": refusal or None}, schema)[1]
        if problem is not None:
            failed(problem, t0, attempt, False, usage)
            raise problem
        return ModelOutput(parsed=json.loads(text), raw_text=text, finish_reason=finish, usage=usage, latency_s=latency, attempts=attempt)
    raise ApiFailure("retries exhausted", kind="unknown", attempts=max_attempts)   # not reachable; keeps the type checker calm



class OpenAIClient:
    """What get_or_call is given: `complete(payload)` plus the attempt reporting hook."""

    reports_attempts = True   # get_or_call hands us a ledger callback because we retry internally

    def __init__(self, sdk, guard: SpendGuard | None = None, sleep=time.sleep, rng=random.random):
        self._sdk = sdk
        self.guard = guard or SpendGuard()
        self._sleep, self._rng = sleep, rng

    def __repr__(self) -> str:   # never show anything key-related
        return f"OpenAIClient(spent=${self.guard.spent:.4f}, limit=${self.guard.limit:.2f})"

    def complete(self, payload: dict, on_attempt=None) -> dict:
        out = call_model(self._sdk, system=payload["system"], user=payload["user"], schema=payload["schema"],
                         model=payload["model"], schema_name=payload.get("schema_name", "rfq_output"),
                         on_attempt=on_attempt, sleep=self._sleep, rng=self._rng, guard=self.guard, strict_reply=False)
        # Cached as received, good or bad: it was billed. Judge it with check_reply() outside the cache.
        return {"response": {"parsed": out.parsed, "raw_text": out.raw_text, "finish_reason": out.finish_reason,
                             "refusal": out.refusal}, "usage": out.usage}


def make_client(api_key: str | None = None, http_client=None, **kw) -> OpenAIClient:
    """Build the client from OPENAI_API_KEY. Raises MissingKey (typed) when there is no key."""
    key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
    if not key or not key.strip():
        raise MissingKey("OPENAI_API_KEY is not set")
    sdk = openai.OpenAI(api_key=key.strip(), max_retries=0, http_client=http_client,
                        timeout=openai.Timeout(config.LLM_TIMEOUT_S, connect=config.LLM_CONNECT_TIMEOUT_S))
    return OpenAIClient(sdk, **kw)
