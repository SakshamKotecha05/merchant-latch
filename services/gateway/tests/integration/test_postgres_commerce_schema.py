from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from acsa.adapters.postgres.models import (
    ApprovalSnapshotRecord,
    AuditEvent,
    CheckoutLine,
    CheckoutSession,
    CommerceIdempotencyRecord,
    Inventory,
    InventoryLease,
    MerchantConfig,
    PaymentAttempt,
    PickupLocation,
    PolicyPack,
    Product,
    Variant,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


async def _insert_catalog(session) -> None:  # type: ignore[no-untyped-def]
    session.add(
        MerchantConfig(
            id="merchant_demo",
            public_name="MerchantLatch",
            currency="INR",
            active_policy_pack_version=1,
        )
    )
    session.add(
        PolicyPack(
            merchant_id="merchant_demo",
            version=1,
            rules={
                "max_quantity_per_line": 2,
                "max_total_quantity": 3,
                "approval_lifetime_seconds": 600,
                "inventory_lease_lifetime_seconds": 600,
            },
        )
    )
    session.add(
        PickupLocation(
            id="pickup_blr_01",
            merchant_id="merchant_demo",
            name="MerchantLatch Bengaluru",
            city="Bengaluru",
            active=True,
        )
    )
    session.add(
        Product(
            id="prod_stride",
            merchant_id="merchant_demo",
            name="Stride One",
            description="A clean everyday sneaker.",
            search_text="stride one clean everyday sneaker",
            active=True,
        )
    )
    session.add(
        Variant(
            id="var_stride_42_black",
            product_id="prod_stride",
            sku="ML-STRIDE-BLK-42",
            size="42",
            color="Black",
            unit_price_minor=499_900,
            currency="INR",
            active=True,
        )
    )
    await session.flush()


async def test_commerce_schema_persists_one_approval_attempt_and_lease(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    async with session_factory() as session, session.begin():
        await _insert_catalog(session)
        session.add(
            Inventory(
                variant_id="var_stride_42_black",
                on_hand=5,
                reserved=1,
                sold=0,
                version=1,
            )
        )
        session.add(
            CheckoutSession(
                id="chk_test_01",
                merchant_id="merchant_demo",
                buyer_key_id="buyer-p256-2026-01",
                status="payment_pending",
                version=1,
                policy_pack_version=1,
                pickup_location_id="pickup_blr_01",
                currency="INR",
                budget_minor=500_000,
                expires_at=NOW + timedelta(minutes=30),
            )
        )
        await session.flush()
        session.add(
            CheckoutLine(
                checkout_id="chk_test_01",
                position=1,
                variant_id="var_stride_42_black",
                quantity=1,
                product_name="Stride One",
                sku="ML-STRIDE-BLK-42",
                size="42",
                color="Black",
                unit_price_minor=499_900,
                inventory_version=1,
            )
        )
        snapshot = ApprovalSnapshotRecord(
            checkout_id="chk_test_01",
            checkout_version=1,
            policy_pack_version=1,
            checksum="a" * 64,
            canonical_body=b"{}",
            approved_by="buyer-p256-2026-01",
            approved_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        session.add(snapshot)
        await session.flush()
        attempt = PaymentAttempt(
            id="pat_test_01",
            checkout_id="chk_test_01",
            attempt_version=1,
            state="draft",
            receipt="acsa1_0123456789abcdefghjkmnpqrs",
            snapshot_id=snapshot.id,
            snapshot_checksum=snapshot.checksum,
            amount_minor=499_900,
            currency="INR",
            provider_uncertain=False,
        )
        session.add(attempt)
        await session.flush()
        session.add(
            InventoryLease(
                attempt_id=attempt.id,
                state="active",
                expires_at=NOW + timedelta(minutes=10),
            )
        )
        session.add(
            CommerceIdempotencyRecord(
                buyer_key_id="buyer-p256-2026-01",
                operation="approve_checkout",
                idempotency_key="idem-approve-01",
                request_sha256="b" * 64,
                checkout_id="chk_test_01",
                response_body=b"{}",
            )
        )
        session.add(
            AuditEvent(
                aggregate_type="checkout",
                aggregate_id="chk_test_01",
                sequence=1,
                event_type="checkout.approved",
                payload={"snapshot_checksum": "a" * 64},
                evidence_source="merchant_browser",
            )
        )


@pytest.mark.parametrize(
    ("on_hand", "reserved", "sold"),
    [(-1, 0, 0), (5, -1, 0), (5, 0, -1), (5, 4, 2)],
)
async def test_inventory_constraints_reject_impossible_quantities(
    session_factory,  # type: ignore[no-untyped-def]
    on_hand: int,
    reserved: int,
    sold: int,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await _insert_catalog(session)
            session.add(
                Inventory(
                    variant_id="var_stride_42_black",
                    on_hand=on_hand,
                    reserved=reserved,
                    sold=sold,
                    version=1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()


async def test_attempt_version_is_unique_per_checkout(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await _insert_catalog(session)
            session.add(
                CheckoutSession(
                    id="chk_test_01",
                    merchant_id="merchant_demo",
                    buyer_key_id="buyer-p256-2026-01",
                    status="requires_buyer_review",
                    version=1,
                    policy_pack_version=1,
                    pickup_location_id="pickup_blr_01",
                    currency="INR",
                    expires_at=NOW + timedelta(minutes=30),
                )
            )
            await session.flush()
            snapshot = ApprovalSnapshotRecord(
                checkout_id="chk_test_01",
                checkout_version=1,
                policy_pack_version=1,
                checksum="a" * 64,
                canonical_body=b"{}",
                approved_by="buyer-p256-2026-01",
                approved_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
            session.add(snapshot)
            await session.flush()
            session.add_all(
                [
                    PaymentAttempt(
                        id=f"pat_test_0{index}",
                        checkout_id="chk_test_01",
                        attempt_version=1,
                        state="draft",
                        receipt=f"acsa1_0123456789abcdefghjkmnpqr{index}",
                        snapshot_id=snapshot.id,
                        snapshot_checksum=snapshot.checksum,
                        amount_minor=499_900,
                        currency="INR",
                        provider_uncertain=False,
                    )
                    for index in (1, 2)
                ]
            )
            with pytest.raises(IntegrityError):
                await session.flush()
