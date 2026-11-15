"""Typed errors for model calls. Nothing here carries an API key or a raw provider message."""

from __future__ import annotations

import re

from .cache import CacheMiss


class LLMCallError(Exception):
    """A model call failed (base class). Nothing is cached for a failed call."""


class MissingKey(Exception):
    """OPENAI_API_KEY is not set. The pipeline continues with no client (cache hits only)."""


class BudgetExceeded(CacheMiss):
    """This run's real-call spend reached MAX_USD_PER_RUN. A CacheMiss, so the pipeline treats it as one."""

    def __init__(self, spent: float, limit: float, model: str = "", key: str = ""):
        Exception.__init__(self, f"spend guard: ${spent:.4f} spent this run, limit ${limit:.2f}; no further real calls")
        self.spent, self.limit, self.model, self.key = spent, limit, model, key


class ModelNotPriced(LLMCallError):
    """The model has no entry in config.MODEL_PRICES, so the spend guard could not count it."""


class ApiFailure(LLMCallError):
    def __init__(self, message: str, *, kind: str, status_code: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.kind, self.status_code, self.attempts = kind, status_code, attempts


class ModelRefusal(LLMCallError):
    def __init__(self, message: str, refusal: str = ""):
        super().__init__(message)
        self.refusal = refusal


class OutputTruncated(LLMCallError):
    """The model stopped at the length limit, so the JSON is incomplete."""


class SchemaMismatch(LLMCallError):
    """The reply was empty, not JSON, or did not match the schema."""


_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-*.]{4,}|Bearer\s+\S+)", re.I)


def redact(text: str, limit: int = 300) -> str:
    """Strip anything shaped like a key or bearer token, and cap the length."""
    t = _SECRET.sub("[redacted]", str(text))
    return t if len(t) <= limit else t[:limit] + "..."
