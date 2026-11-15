"""The extraction schema: what the model returns for one email (strict structured outputs)."""

from __future__ import annotations

CLASSIFICATIONS = ["rfq", "rfq_revision", "purchase_order", "vendor_quote", "status_inquiry", "auto_reply", "other"]
REQUEST_CLASSES = ("rfq", "rfq_revision", "purchase_order")
UOMS = ["EA", "FT", "M", "IN", "BX", "PK", "PR", "DZ", "RL", "LB", "SET", "GAL", "BAG", "CS"]
DOUBT_REASONS = ["illegible", "attachment_missing_or_unreadable", "item_defined_outside_email", "unclear_if_request", "other"]

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classification", "revises_hint", "lines", "doubts", "note"],
    "properties": {
        "classification": {"type": "string", "enum": CLASSIFICATIONS},
        "revises_hint": {"type": ["string", "null"]},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["raw", "description", "qty", "uom", "source"],
                "properties": {
                    "raw": {"type": "string"},
                    "description": {"type": "string"},
                    "qty": {"type": ["number", "null"]},
                    "uom": {"type": ["string", "null"], "enum": UOMS + [None]},
                    "source": {"type": "string"},
                },
            },
        },
        "doubts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["reason", "detail"],
                "properties": {"reason": {"type": "string", "enum": DOUBT_REASONS}, "detail": {"type": "string"}},
            },
        },
        "note": {"type": "string"},
    },
}
SCHEMA_NAME = "rfq_extraction"
