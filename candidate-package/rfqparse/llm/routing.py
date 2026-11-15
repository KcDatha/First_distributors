"""Which model answers an email, and the one-retry escalation. Config driven (config.ROUTING).

Routing looks only at what is actually in the payload: an image part means the image model, whatever the
attachments were. A spreadsheet or PDF rendered to text by code is text; a skipped tracking pixel never
makes it into the payload, so it does not count. Nothing is decided from an image's name or content.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from .cache import Cache
from .call import CallResult, CallStats, get_or_call
from .client import build_payload, check_reply
from .ledger import Ledger


def route_model(user_parts: list[dict]) -> str:
    """The model for this payload: image model if any user part is an image, else the text model."""
    has_image = any(isinstance(p, dict) and p.get("type") == "image" for p in user_parts)
    return config.ROUTING["image_model"] if has_image else config.ROUTING["text_model"]


def escalation_model(model: str) -> str | None:
    """Where a failed answer is retried once: only the text model escalates; the image model is already the top."""
    target = config.ROUTING.get("escalate_text_model_to")
    return target if model == config.ROUTING["text_model"] and target and target != model else None


@dataclass
class RoutedResult:
    parsed: object
    model: str                     # the model whose answer was accepted
    escalated: bool
    results: list[tuple[str, CallResult]] = field(default_factory=list)   # every attempt, in order: (model, result)

    @property
    def usd(self) -> float | None:
        vals = [r.usd for _, r in self.results]
        return None if any(v is None for v in vals) else round(sum(vals), 6)


def call_routed(*, system: str, user: list[dict], schema: dict, schema_name: str = "rfq_output", client,
                email_id: str, prompt_version: str, schema_version: str, validate=None, repair=None,
                cache: Cache | None = None, ledger: Ledger | None = None, stats: CallStats | None = None) -> RoutedResult:
    """Route, call through the cache, validate OUTSIDE the cache, and escalate once if the text model's answer fails.

    Both attempts go through get_or_call, so both are cached (a bad answer included). A replay with no key therefore
    takes exactly the same path: first answer from the cache, same verdict, same second answer from the cache.
    Raises the typed error of the last attempt when no answer validates. API-level failures, CacheMiss and
    BudgetExceeded propagate unchanged and never trigger escalation.
    """
    model = route_model(user)
    attempts: list[tuple[str, CallResult]] = []
    while True:
        payload = build_payload(model=model, system=system, user=user, schema=schema, schema_name=schema_name)
        res = get_or_call(payload, client, model=model, prompt_version=prompt_version, schema_version=schema_version,
                          email_id=email_id, cache=cache, ledger=ledger, stats=stats)
        attempts.append((model, res))
        parsed, err = check_reply(res.response, schema, validate, repair)
        if err is None:
            return RoutedResult(parsed=parsed, model=model, escalated=len(attempts) > 1, results=attempts)
        nxt = escalation_model(model) if len(attempts) == 1 else None
        if nxt is None:
            err.attempts = attempts          # type: ignore[attr-defined]  (for diagnostics)
            raise err
        model = nxt
