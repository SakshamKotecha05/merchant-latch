from __future__ import annotations

from acsa.domain.receipts import ProviderOrderCandidate, build_receipt, select_exact_order


def test_receipt_is_stable_and_below_provider_limit() -> None:
    receipt = build_receipt(
        merchant_id="merchant_demo",
        payment_attempt_id="attempt_01",
        snapshot_checksum="sha256:abc123",
    )

    assert receipt == "acsa1_ban5j6l3f3rkz5vccrs4ckfsd2"
    assert len(receipt) <= 40


def test_exact_order_filter_rejects_contains_matches() -> None:
    expected = ProviderOrderCandidate(
        order_id="order_exact",
        receipt="acsa1_exact",
        amount_minor=100,
        currency="INR",
        notes={"attempt_id": "attempt_01", "snapshot_checksum": "checksum_01"},
    )
    candidates = [
        ProviderOrderCandidate(
            order_id="order_contains",
            receipt="prefix_acsa1_exact_suffix",
            amount_minor=100,
            currency="INR",
            notes={"attempt_id": "attempt_01", "snapshot_checksum": "checksum_01"},
        ),
        expected,
    ]

    selected = select_exact_order(
        candidates,
        expected_receipt="acsa1_exact",
        expected_amount_minor=100,
        expected_currency="INR",
        expected_notes={"attempt_id": "attempt_01", "snapshot_checksum": "checksum_01"},
    )

    assert selected == expected


def test_conflicting_exact_orders_are_ambiguous() -> None:
    candidates = [
        ProviderOrderCandidate(
            order_id=order_id,
            receipt="acsa1_exact",
            amount_minor=100,
            currency="INR",
            notes={"attempt_id": "attempt_01", "snapshot_checksum": "checksum_01"},
        )
        for order_id in ("order_a", "order_b")
    ]

    selected = select_exact_order(
        candidates,
        expected_receipt="acsa1_exact",
        expected_amount_minor=100,
        expected_currency="INR",
        expected_notes={"attempt_id": "attempt_01", "snapshot_checksum": "checksum_01"},
    )

    assert selected is None
