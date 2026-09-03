"""UCP checkout resource construction for the initial escalation-only slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import orjson

UCP_VERSION = "2026-04-08"
SHOPPING_SERVICE_PATH = "/ucp/shopping"


@dataclass(frozen=True, slots=True)
class StoredCheckout:
    """The immutable checkout representation persisted by the gateway."""

    id: str
    buyer_key_id: str
    status: str
    continue_url: str
    expires_at: datetime
    resource: dict[str, Any]
    response_body: bytes


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    """Serialize a UCP resource once for storage, response, and replay."""
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def create_escalated_checkout(
    *,
    checkout_id: str,
    buyer_key_id: str,
    line_items: list[dict[str, Any]],
    continue_url: str,
    expires_at: datetime,
) -> StoredCheckout:
    """Create the only Phase 1 checkout state: merchant-controlled escalation."""
    resource: dict[str, Any] = {
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": {
                "dev.ucp.shopping.checkout": [{"version": UCP_VERSION}],
            },
        },
        "id": checkout_id,
        "status": "requires_escalation",
        "line_items": line_items,
        "messages": [
            {
                "type": "error",
                "code": "merchant_review_required",
                "content": "Continue in the merchant checkout to review and authorize payment.",
                "severity": "requires_buyer_review",
            }
        ],
        "continue_url": continue_url,
    }
    return StoredCheckout(
        id=checkout_id,
        buyer_key_id=buyer_key_id,
        status="requires_escalation",
        continue_url=continue_url,
        expires_at=expires_at,
        resource=resource,
        response_body=canonical_json_bytes(resource),
    )
