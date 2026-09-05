from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from acsa.adapters.postgres.commerce import PostgresCommerceStore
from acsa.adapters.postgres.models import (
    ApprovalSnapshotRecord,
    CheckoutLine,
    CheckoutSession,
    Inventory,
    InventoryLease,
    MerchantConfig,
    OutboxJob,
    PaymentAttempt,
    PickupLocation,
    PolicyPack,
    Product,
    Variant,
)
from acsa.domain.commerce import ApprovalOutcome, RequestedLine

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


async def _seed_checkout(session_factory) -> PostgresCommerceStore:  # type: ignore[no-untyped-def]
    async with session_factory() as session, session.begin():
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
        session.add(Inventory(variant_id="var_stride_42_black", on_hand=5, version=3))

    store = PostgresCommerceStore(session_factory)
    created = await store.create_checkout(
        checkout_id="chk_test_01",
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )
    assert created.checkout is not None
    return store


async def test_approval_atomically_reserves_inventory_and_enqueues_one_provider_job(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = await _seed_checkout(session_factory)
    preview = await store.preview_approval(
        checkout_id="chk_test_01",
        expected_version=1,
        approved_at=NOW,
    )
    assert preview.snapshot is not None

    result = await store.approve_checkout(
        checkout_id="chk_test_01",
        expected_version=1,
        snapshot_checksum=preview.snapshot.checksum,
        idempotency_key="idem-approve-01",
        request_sha256="b" * 64,
        approved_at=NOW,
    )

    assert result.outcome is ApprovalOutcome.APPROVED
    assert result.attempt_id is not None
    assert result.outbox_job_id is not None
    async with session_factory() as session:
        inventory = await session.get(Inventory, "var_stride_42_black")
        checkout = await session.get(CheckoutSession, "chk_test_01")
        assert inventory is not None and inventory.reserved == 1 and inventory.version == 4
        assert checkout is not None and checkout.status == "approved" and checkout.version == 2
        assert await session.scalar(select(func.count()).select_from(ApprovalSnapshotRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1
        assert await session.scalar(select(func.count()).select_from(InventoryLease)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 1
        job = await session.scalar(select(OutboxJob))
        assert job is not None and job.max_attempts == 6
        assert job.id == result.outbox_job_id


async def test_duplicate_approval_creates_one_attempt_lease_and_provider_job(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = await _seed_checkout(session_factory)
    preview = await store.preview_approval(
        checkout_id="chk_test_01",
        expected_version=1,
        approved_at=NOW,
    )
    assert preview.snapshot is not None

    async def approve():  # type: ignore[no-untyped-def]
        return await store.approve_checkout(
            checkout_id="chk_test_01",
            expected_version=1,
            snapshot_checksum=preview.snapshot.checksum,
            idempotency_key="idem-approve-01",
            request_sha256="b" * 64,
            approved_at=NOW,
        )

    first, second = await asyncio.gather(approve(), approve())

    assert {first.outcome, second.outcome} == {
        ApprovalOutcome.APPROVED,
        ApprovalOutcome.REPLAYED,
    }
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1
        assert await session.scalar(select(func.count()).select_from(InventoryLease)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 1


async def test_snapshot_drift_creates_zero_provider_action(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = await _seed_checkout(session_factory)
    preview = await store.preview_approval(
        checkout_id="chk_test_01",
        expected_version=1,
        approved_at=NOW,
    )
    assert preview.snapshot is not None
    async with session_factory() as session, session.begin():
        inventory = await session.get(Inventory, "var_stride_42_black")
        assert inventory is not None
        inventory.version += 1

    result = await store.approve_checkout(
        checkout_id="chk_test_01",
        expected_version=1,
        snapshot_checksum=preview.snapshot.checksum,
        idempotency_key="idem-approve-01",
        request_sha256="b" * 64,
        approved_at=NOW,
    )

    assert result.outcome is ApprovalOutcome.BLOCKED
    assert result.rule_ids == ("inventory_version_changed",)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0
        assert await session.scalar(select(func.count()).select_from(InventoryLease)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 0


@pytest.mark.parametrize(
    ("drift", "expected_outcome", "expected_rule"),
    [
        ("price", ApprovalOutcome.BLOCKED, "price_changed"),
        ("inventory_count", ApprovalOutcome.BLOCKED, "inventory_insufficient"),
        ("policy", ApprovalOutcome.BLOCKED, "policy_version_changed"),
        ("pickup", ApprovalOutcome.BLOCKED, "pickup_location_changed"),
        ("checkout_version", ApprovalOutcome.STALE, None),
        ("quantity", ApprovalOutcome.BLOCKED, "snapshot_checksum_changed"),
        ("expired", ApprovalOutcome.BLOCKED, "checkout_expired"),
        ("canceled", ApprovalOutcome.BLOCKED, "checkout_not_approvable"),
        ("completed", ApprovalOutcome.BLOCKED, "checkout_not_approvable"),
    ],
)
async def test_authoritative_drift_never_creates_provider_action(
    session_factory,  # type: ignore[no-untyped-def]
    drift: str,
    expected_outcome: ApprovalOutcome,
    expected_rule: str | None,
) -> None:
    store = await _seed_checkout(session_factory)
    preview = await store.preview_approval(
        checkout_id="chk_test_01",
        expected_version=1,
        approved_at=NOW,
    )
    assert preview.snapshot is not None
    async with session_factory() as session, session.begin():
        checkout = await session.get(CheckoutSession, "chk_test_01")
        assert checkout is not None
        if drift == "price":
            variant = await session.get(Variant, "var_stride_42_black")
            assert variant is not None
            variant.unit_price_minor += 10_000
        elif drift == "inventory_count":
            inventory = await session.get(Inventory, "var_stride_42_black")
            assert inventory is not None
            inventory.on_hand = 0
        elif drift == "policy":
            checkout.merchant_id = "merchant_demo"
            merchant = await session.get(MerchantConfig, "merchant_demo")
            assert merchant is not None
            merchant.active_policy_pack_version = 2
        elif drift == "pickup":
            pickup = await session.get(PickupLocation, "pickup_blr_01")
            assert pickup is not None
            pickup.active = False
        elif drift == "checkout_version":
            checkout.version = 2
        elif drift == "quantity":
            line = await session.scalar(
                select(CheckoutLine).where(CheckoutLine.checkout_id == "chk_test_01")
            )
            assert line is not None
            line.quantity = 2
        elif drift == "expired":
            checkout.expires_at = NOW - timedelta(seconds=1)
        else:
            checkout.status = drift

    result = await store.approve_checkout(
        checkout_id="chk_test_01",
        expected_version=1,
        snapshot_checksum=preview.snapshot.checksum,
        idempotency_key="idem-approve-01",
        request_sha256="b" * 64,
        approved_at=NOW,
    )

    assert result.outcome is expected_outcome
    assert result.rule_ids == ((expected_rule,) if expected_rule is not None else ())
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0
        assert await session.scalar(select(func.count()).select_from(InventoryLease)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 0


async def test_wrong_snapshot_checksum_creates_zero_provider_action(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = await _seed_checkout(session_factory)

    result = await store.approve_checkout(
        checkout_id="chk_test_01",
        expected_version=1,
        snapshot_checksum="0" * 64,
        idempotency_key="idem-approve-01",
        request_sha256="b" * 64,
        approved_at=NOW,
    )

    assert result.outcome is ApprovalOutcome.BLOCKED
    assert result.rule_ids == ("snapshot_checksum_changed",)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0
        assert await session.scalar(select(func.count()).select_from(InventoryLease)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 0


async def test_currency_drift_is_rejected_before_provider_action(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_checkout(session_factory)

    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            variant = await session.get(Variant, "var_stride_42_black")
            assert variant is not None
            variant.currency = "USD"

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0
        assert await session.scalar(select(func.count()).select_from(InventoryLease)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 0
