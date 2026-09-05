"""Merchant-authoritative commerce values and deterministic rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from acsa.domain.canonical import canonical_json_bytes, sha256_checksum

MAX_MINOR_AMOUNT = 9_223_372_036_854_775_807


class GateVerdict(StrEnum):
    ALLOW = "allow"
    CLARIFY = "clarify"
    BLOCK = "block"
    MANUAL_REVIEW = "manual_review"


class CheckoutStatus(StrEnum):
    OPEN = "open"
    REQUIRES_BUYER_REVIEW = "requires_buyer_review"
    APPROVED = "approved"
    PAYMENT_PENDING = "payment_pending"
    COMPLETED = "completed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    MANUAL_REVIEW = "manual_review"


class PaymentAttemptState(StrEnum):
    DRAFT = "draft"
    PROVIDER_ORDER_CREATING = "provider_order_creating"
    AWAITING_PAYMENT = "awaiting_payment"
    VERIFYING = "verifying"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"
    RECONCILING = "reconciling"
    PAID_INVENTORY_EXCEPTION = "paid_inventory_exception"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    MANUAL_REVIEW = "manual_review"
    CANCELED = "canceled"


class InventoryLeaseState(StrEnum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


class CommerceMutationOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    CANCELED = "canceled"
    REPLAYED = "replayed"
    CONFLICT = "conflict"
    STALE = "stale"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"


class ApprovalOutcome(StrEnum):
    READY = "ready"
    APPROVED = "approved"
    REPLAYED = "replayed"
    CONFLICT = "conflict"
    STALE = "stale"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class PolicyRules:
    max_quantity_per_line: int
    max_total_quantity: int
    approval_lifetime: timedelta
    inventory_lease_lifetime: timedelta


@dataclass(frozen=True, slots=True)
class RequestedLine:
    variant_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class VariantTerms:
    id: str
    sku: str
    product_name: str
    size: str
    color: str
    unit_price_minor: int
    inventory_version: int
    available_quantity: int
    active: bool = True
    currency: str = "INR"


@dataclass(frozen=True, slots=True)
class PricedLine:
    variant_id: str
    sku: str
    product_name: str
    size: str
    color: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int
    inventory_version: int


@dataclass(frozen=True, slots=True)
class CheckoutPricing:
    currency: str
    item_total_minor: int
    pickup_charge_minor: int
    total_minor: int
    tax_inclusive: bool


@dataclass(frozen=True, slots=True)
class ApprovalSnapshot:
    resource: dict[str, Any]
    canonical_bytes: bytes
    checksum: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GateDecision:
    verdict: GateVerdict
    rule_ids: tuple[str, ...]
    explanation: str
    checkout_version: int
    policy_pack_version: int
    snapshot_checksum: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogVariant:
    id: str
    product_id: str
    product_name: str
    sku: str
    size: str
    color: str
    unit_price_minor: int
    currency: str
    available_quantity: int
    inventory_version: int


@dataclass(frozen=True, slots=True)
class CatalogItem:
    id: str
    name: str
    description: str
    variants: tuple[CatalogVariant, ...]


@dataclass(frozen=True, slots=True)
class CatalogPage:
    items: tuple[CatalogItem, ...]
    next_product_id: str | None


@dataclass(frozen=True, slots=True)
class AuthoritativeCheckout:
    id: str
    buyer_key_id: str
    status: CheckoutStatus
    version: int
    policy_pack_version: int
    pickup_location_id: str
    lines: tuple[PricedLine, ...]
    pricing: CheckoutPricing
    budget_minor: int | None
    expires_at: datetime
    continue_url: str
    canonical_bytes: bytes


@dataclass(frozen=True, slots=True)
class CommerceMutationResult:
    outcome: CommerceMutationOutcome
    checkout: AuthoritativeCheckout | None = None
    response_body: bytes | None = None
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalPreview:
    outcome: ApprovalOutcome
    snapshot: ApprovalSnapshot | None = None
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    outcome: ApprovalOutcome
    attempt_id: str | None = None
    response_body: bytes | None = None
    rule_ids: tuple[str, ...] = ()
    outbox_job_id: UUID | None = None


class CommerceRuleViolation(ValueError):
    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id
        super().__init__(rule_id)


_CHECKOUT_TRANSITIONS = frozenset(
    {
        (CheckoutStatus.OPEN, CheckoutStatus.REQUIRES_BUYER_REVIEW),
        (CheckoutStatus.OPEN, CheckoutStatus.CANCELED),
        (CheckoutStatus.OPEN, CheckoutStatus.EXPIRED),
        (CheckoutStatus.REQUIRES_BUYER_REVIEW, CheckoutStatus.APPROVED),
        (CheckoutStatus.REQUIRES_BUYER_REVIEW, CheckoutStatus.CANCELED),
        (CheckoutStatus.REQUIRES_BUYER_REVIEW, CheckoutStatus.EXPIRED),
        (CheckoutStatus.APPROVED, CheckoutStatus.PAYMENT_PENDING),
        (CheckoutStatus.APPROVED, CheckoutStatus.CANCELED),
        (CheckoutStatus.APPROVED, CheckoutStatus.EXPIRED),
        (CheckoutStatus.PAYMENT_PENDING, CheckoutStatus.COMPLETED),
        (CheckoutStatus.PAYMENT_PENDING, CheckoutStatus.CANCELED),
        (CheckoutStatus.PAYMENT_PENDING, CheckoutStatus.EXPIRED),
        (CheckoutStatus.PAYMENT_PENDING, CheckoutStatus.MANUAL_REVIEW),
    }
)

_ATTEMPT_TRANSITIONS = frozenset(
    {
        (PaymentAttemptState.DRAFT, PaymentAttemptState.PROVIDER_ORDER_CREATING),
        (PaymentAttemptState.DRAFT, PaymentAttemptState.CANCELED),
        (PaymentAttemptState.DRAFT, PaymentAttemptState.EXPIRED),
        (
            PaymentAttemptState.PROVIDER_ORDER_CREATING,
            PaymentAttemptState.AWAITING_PAYMENT,
        ),
        (PaymentAttemptState.PROVIDER_ORDER_CREATING, PaymentAttemptState.RECONCILING),
        (PaymentAttemptState.PROVIDER_ORDER_CREATING, PaymentAttemptState.FAILED),
        (PaymentAttemptState.AWAITING_PAYMENT, PaymentAttemptState.VERIFYING),
        (PaymentAttemptState.AWAITING_PAYMENT, PaymentAttemptState.CANCELED),
        (PaymentAttemptState.AWAITING_PAYMENT, PaymentAttemptState.EXPIRED),
        (PaymentAttemptState.RECONCILING, PaymentAttemptState.AWAITING_PAYMENT),
        (PaymentAttemptState.RECONCILING, PaymentAttemptState.VERIFYING),
        (PaymentAttemptState.RECONCILING, PaymentAttemptState.MANUAL_REVIEW),
        (PaymentAttemptState.RECONCILING, PaymentAttemptState.FAILED),
        (PaymentAttemptState.VERIFYING, PaymentAttemptState.PAID),
        (PaymentAttemptState.VERIFYING, PaymentAttemptState.RECONCILING),
        (PaymentAttemptState.VERIFYING, PaymentAttemptState.PAID_INVENTORY_EXCEPTION),
        (PaymentAttemptState.VERIFYING, PaymentAttemptState.MANUAL_REVIEW),
        (PaymentAttemptState.VERIFYING, PaymentAttemptState.FAILED),
        (PaymentAttemptState.PAID_INVENTORY_EXCEPTION, PaymentAttemptState.REFUND_PENDING),
        (PaymentAttemptState.REFUND_PENDING, PaymentAttemptState.REFUNDED),
        (PaymentAttemptState.REFUND_PENDING, PaymentAttemptState.MANUAL_REVIEW),
        (PaymentAttemptState.EXPIRED, PaymentAttemptState.VERIFYING),
        (PaymentAttemptState.CANCELED, PaymentAttemptState.VERIFYING),
    }
)


def checkout_transition_allowed(source: CheckoutStatus, target: CheckoutStatus) -> bool:
    return (source, target) in _CHECKOUT_TRANSITIONS


def attempt_transition_allowed(source: PaymentAttemptState, target: PaymentAttemptState) -> bool:
    return (source, target) in _ATTEMPT_TRANSITIONS


def price_checkout(
    *,
    requests: Sequence[RequestedLine],
    variants: Mapping[str, VariantTerms],
    rules: PolicyRules,
    budget_minor: int | None = None,
) -> tuple[list[PricedLine], CheckoutPricing]:
    if not requests:
        raise CommerceRuleViolation("line_items_required")
    if budget_minor is not None and (isinstance(budget_minor, bool) or budget_minor < 0):
        raise CommerceRuleViolation("budget_invalid")

    total_quantity = 0
    item_total_minor = 0
    seen_variants: set[str] = set()
    priced_lines: list[PricedLine] = []
    for request in requests:
        if request.variant_id in seen_variants:
            raise CommerceRuleViolation("duplicate_variant")
        seen_variants.add(request.variant_id)
        variant = variants.get(request.variant_id)
        if variant is None:
            raise CommerceRuleViolation("variant_not_found")
        if not variant.active:
            raise CommerceRuleViolation("variant_inactive")
        if variant.currency != "INR":
            raise CommerceRuleViolation("currency_not_supported")
        if variant.unit_price_minor <= 0:
            raise CommerceRuleViolation("catalog_price_invalid")
        if variant.inventory_version <= 0 or variant.available_quantity < 0:
            raise CommerceRuleViolation("inventory_state_invalid")
        if isinstance(request.quantity, bool) or request.quantity <= 0:
            raise CommerceRuleViolation("quantity_non_positive")
        if request.quantity > rules.max_quantity_per_line:
            raise CommerceRuleViolation("quantity_per_line_exceeded")
        if request.quantity > variant.available_quantity:
            raise CommerceRuleViolation("inventory_insufficient")

        total_quantity += request.quantity
        if total_quantity > rules.max_total_quantity:
            raise CommerceRuleViolation("total_quantity_exceeded")
        line_total_minor = variant.unit_price_minor * request.quantity
        item_total_minor += line_total_minor
        if item_total_minor > MAX_MINOR_AMOUNT:
            raise CommerceRuleViolation("total_out_of_range")
        priced_lines.append(
            PricedLine(
                variant_id=variant.id,
                sku=variant.sku,
                product_name=variant.product_name,
                size=variant.size,
                color=variant.color,
                quantity=request.quantity,
                unit_price_minor=variant.unit_price_minor,
                line_total_minor=line_total_minor,
                inventory_version=variant.inventory_version,
            )
        )

    if budget_minor is not None and item_total_minor > budget_minor:
        raise CommerceRuleViolation("budget_exceeded")
    pricing = CheckoutPricing(
        currency="INR",
        item_total_minor=item_total_minor,
        pickup_charge_minor=0,
        total_minor=item_total_minor,
        tax_inclusive=True,
    )
    return priced_lines, pricing


def build_approval_snapshot(
    *,
    merchant_id: str,
    checkout_id: str,
    checkout_version: int,
    policy_pack_version: int,
    lines: Sequence[PricedLine],
    pricing: CheckoutPricing,
    pickup_location_id: str,
    approved_by: str,
    approved_at: datetime,
    expires_at: datetime,
) -> ApprovalSnapshot:
    resource: dict[str, Any] = {
        "snapshotVersion": 1,
        "merchantId": merchant_id,
        "checkoutId": checkout_id,
        "checkoutVersion": checkout_version,
        "policyPackVersion": policy_pack_version,
        "lines": [
            {
                "variantId": line.variant_id,
                "sku": line.sku,
                "name": line.product_name,
                "size": line.size,
                "color": line.color,
                "quantity": line.quantity,
                "unitPriceMinor": line.unit_price_minor,
                "inventoryVersion": line.inventory_version,
            }
            for line in lines
        ],
        "pricing": {
            "currency": pricing.currency,
            "itemTotalMinor": pricing.item_total_minor,
            "pickupChargeMinor": pricing.pickup_charge_minor,
            "totalMinor": pricing.total_minor,
            "taxInclusive": pricing.tax_inclusive,
        },
        "fulfillment": {
            "type": "store_pickup",
            "locationId": pickup_location_id,
        },
        "approvedBy": approved_by,
        "approvedAt": _utc_isoformat(approved_at),
        "expiresAt": _utc_isoformat(expires_at),
    }
    return ApprovalSnapshot(
        resource=resource,
        canonical_bytes=canonical_json_bytes(resource),
        checksum=sha256_checksum(resource),
        expires_at=expires_at,
    )


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CommerceRuleViolation("timestamp_timezone_required")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
