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


def clean_cell(s: str, limit: int = 500) -> str:
    """One line per cell: newlines become ' / ', and very long cells are cut and marked."""
    s = " / ".join(p.strip() for p in str(s).replace("\r", "").split("\n") if p.strip())
    return s if len(s) <= limit else s[:limit] + "...[cell cut]"
