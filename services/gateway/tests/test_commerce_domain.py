from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from acsa.domain.canonical import canonical_json_bytes, sha256_checksum
from acsa.domain.commerce import (
    CheckoutStatus,
    CommerceRuleViolation,
    InventoryLeaseState,
    PaymentAttemptState,
    PolicyRules,
    RequestedLine,
    VariantTerms,
    attempt_transition_allowed,
    build_approval_snapshot,
    checkout_transition_allowed,
    price_checkout,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _variant(**changes: object) -> VariantTerms:
    values: dict[str, object] = {
        "id": "var_stride_42_black",
        "sku": "ML-STRIDE-BLK-42",
        "product_name": "Stride One",
        "size": "42",
        "color": "Black",
        "unit_price_minor": 499_900,
        "inventory_version": 3,
        "available_quantity": 5,
        "active": True,
        "currency": "INR",
    }
    values.update(changes)
    return VariantTerms(**values)  # type: ignore[arg-type]


def _rules() -> PolicyRules:
    return PolicyRules(
        max_quantity_per_line=2,
        max_total_quantity=3,
        approval_lifetime=timedelta(minutes=10),
        inventory_lease_lifetime=timedelta(minutes=10),
    )


def test_price_checkout_uses_only_authoritative_variant_terms() -> None:
    lines, pricing = price_checkout(
        requests=[RequestedLine(variant_id="var_stride_42_black", quantity=2)],
        variants={"var_stride_42_black": _variant()},
        rules=_rules(),
        budget_minor=1_000_000,
    )

    assert lines[0].sku == "ML-STRIDE-BLK-42"
    assert lines[0].unit_price_minor == 499_900
    assert lines[0].line_total_minor == 999_800
    assert lines[0].inventory_version == 3
    assert pricing.item_total_minor == 999_800
    assert pricing.pickup_charge_minor == 0
    assert pricing.total_minor == 999_800
    assert pricing.currency == "INR"
    assert pricing.tax_inclusive is True


@pytest.mark.parametrize(
    ("requests", "variants", "budget_minor", "rule_id"),
    [
        ([RequestedLine("var_missing", 1)], {}, None, "variant_not_found"),
        (
            [RequestedLine("var_stride_42_black", 1)],
            {"var_stride_42_black": _variant(active=False)},
            None,
            "variant_inactive",
        ),
        (
            [RequestedLine("var_stride_42_black", 0)],
            {"var_stride_42_black": _variant()},
            None,
            "quantity_non_positive",
        ),
        (
            [RequestedLine("var_stride_42_black", 3)],
            {"var_stride_42_black": _variant()},
            None,
            "quantity_per_line_exceeded",
        ),
        (
            [RequestedLine("var_stride_42_black", 2), RequestedLine("var_court_41_stone", 2)],
            {
                "var_stride_42_black": _variant(),
                "var_court_41_stone": _variant(id="var_court_41_stone", sku="ML-COURT-STN-41"),
            },
            None,
            "total_quantity_exceeded",
        ),
        (
            [RequestedLine("var_stride_42_black", 2)],
            {"var_stride_42_black": _variant(available_quantity=1)},
            None,
            "inventory_insufficient",
        ),
        (
            [RequestedLine("var_stride_42_black", 1)],
            {"var_stride_42_black": _variant(currency="USD")},
            None,
            "currency_not_supported",
        ),
        (
            [RequestedLine("var_stride_42_black", 1)],
            {"var_stride_42_black": _variant()},
            499_899,
            "budget_exceeded",
        ),
        (
            [RequestedLine("var_stride_42_black", 1), RequestedLine("var_stride_42_black", 1)],
            {"var_stride_42_black": _variant()},
            None,
            "duplicate_variant",
        ),
    ],
)
def test_price_checkout_rejects_an_invalid_merchant_rule(
    requests: list[RequestedLine],
    variants: dict[str, VariantTerms],
    budget_minor: int | None,
    rule_id: str,
) -> None:
    with pytest.raises(CommerceRuleViolation) as caught:
        price_checkout(
            requests=requests,
            variants=variants,
            rules=_rules(),
            budget_minor=budget_minor,
        )

    assert caught.value.rule_id == rule_id


def test_approval_snapshot_has_stable_canonical_bytes_and_checksum() -> None:
    lines, pricing = price_checkout(
        requests=[RequestedLine("var_stride_42_black", 1)],
        variants={"var_stride_42_black": _variant()},
        rules=_rules(),
    )

    snapshot = build_approval_snapshot(
        merchant_id="merchant_demo",
        checkout_id="chk_test_01",
        checkout_version=2,
        policy_pack_version=1,
        lines=lines,
        pricing=pricing,
        pickup_location_id="pickup_blr_01",
        approved_by="buyer-p256-2026-01",
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    expected = (
        b'{"approvedAt":"2026-09-04T12:00:00Z","approvedBy":"buyer-p256-2026-01",'
        b'"checkoutId":"chk_test_01","checkoutVersion":2,"expiresAt":"2026-09-04T12:10:00Z",'
        b'"fulfillment":{"locationId":"pickup_blr_01","type":"store_pickup"},'
        b'"lines":[{"color":"Black","inventoryVersion":3,"name":"Stride One",'
        b'"quantity":1,"size":"42","sku":"ML-STRIDE-BLK-42","unitPriceMinor":499900,'
        b'"variantId":"var_stride_42_black"}],"merchantId":"merchant_demo",'
        b'"policyPackVersion":1,"pricing":{"currency":"INR","itemTotalMinor":499900,'
        b'"pickupChargeMinor":0,"taxInclusive":true,"totalMinor":499900},"snapshotVersion":1}'
    )
    expected_checksum = hashlib.sha256(expected).hexdigest()

    assert snapshot.resource["checkoutVersion"] == 2
    assert snapshot.canonical_bytes == expected
    assert snapshot.checksum == expected_checksum
    assert canonical_json_bytes(snapshot.resource) == expected
    assert sha256_checksum(snapshot.resource) == expected_checksum


def test_completed_checkout_has_no_outgoing_transition() -> None:
    assert not any(
        checkout_transition_allowed(CheckoutStatus.COMPLETED, target) for target in CheckoutStatus
    )


def test_paid_attempt_cannot_regress_to_verifying() -> None:
    assert attempt_transition_allowed(PaymentAttemptState.VERIFYING, PaymentAttemptState.PAID)
    assert not attempt_transition_allowed(PaymentAttemptState.PAID, PaymentAttemptState.VERIFYING)


def test_reconciled_provider_order_can_enter_payment_verification() -> None:
    assert attempt_transition_allowed(
        PaymentAttemptState.RECONCILING,
        PaymentAttemptState.VERIFYING,
    )


@pytest.mark.parametrize(
    "state",
    [PaymentAttemptState.EXPIRED, PaymentAttemptState.CANCELED],
)
def test_late_capture_can_verify_after_attempt_termination(
    state: PaymentAttemptState,
) -> None:
    assert attempt_transition_allowed(
        state,
        PaymentAttemptState.VERIFYING,
    )


def test_inventory_lease_states_are_exhaustive_for_the_planned_lifecycle() -> None:
    assert {state.value for state in InventoryLeaseState} == {
        "active",
        "consumed",
        "released",
        "expired",
    }
