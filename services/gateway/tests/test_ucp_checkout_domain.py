from __future__ import annotations

from datetime import UTC, datetime

from acsa.domain.ucp_checkout import canonical_json_bytes, create_escalated_checkout


def test_escalated_checkout_is_canonical_and_contains_the_required_handoff() -> None:
    checkout = create_escalated_checkout(
        checkout_id="chk_test_01",
        buyer_key_id="buyer-p256-2026-01",
        line_items=[{"item": {"id": "sku_test"}, "quantity": 1}],
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=datetime(2026, 9, 5, tzinfo=UTC),
    )

    assert checkout.status == "requires_escalation"
    assert checkout.response_body == canonical_json_bytes(checkout.resource)
    assert checkout.resource["messages"] == [
        {
            "type": "error",
            "code": "merchant_review_required",
            "content": "Continue in the merchant checkout to review and authorize payment.",
            "severity": "requires_buyer_review",
        }
    ]
    assert checkout.resource["continue_url"] == "https://merchant.example/checkout/chk_test_01"
