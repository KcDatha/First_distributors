"""Renderers: attachment bytes -> text a model can read. Each returns a RenderResult and never raises."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RenderResult:
    status: str                      # "ok" or "unreadable"
    text: str = ""
    truncated: bool = False          # output was cut to fit the caps (the text says where)
    reason: str | None = None        # why, when status == "unreadable"
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def unreadable(reason: str) -> RenderResult:
    return RenderResult(status="unreadable", reason=reason)
