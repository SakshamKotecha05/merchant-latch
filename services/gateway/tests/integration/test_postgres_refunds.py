from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from acsa.adapters.postgres.models import (
    ApprovalSnapshotRecord,
    CheckoutLine,
    CheckoutSession,
    Inventory,
    InventoryLease,
    MerchantConfig,
    PaymentAttempt,
    PickupLocation,
    Product,
    Refund,
    Variant,
    WebhookEvent,
)
from acsa.adapters.postgres.refunds import PostgresRefundStore
from acsa.adapters.postgres.webhooks import PostgresWebhookStore
from acsa.domain.payments import ProviderRefundRecord
from acsa.services.refunds import RefundOutcome, RefundService

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC)


async def _seed_attempt(
    session_factory,  # type: ignore[no-untyped-def]
    *,
    late_capture: bool,
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            MerchantConfig(
                id="merchant_demo",
                public_name="MerchantLatch",
                currency="INR",
                active_policy_pack_version=1,
            )
        )
        await session.flush()
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
                id="prod_1",
                merchant_id="merchant_demo",
                name="MerchantLatch Tee",
                description="Test product",
                search_text="merchantlatch tee",
                active=True,
            )
        )
        await session.flush()
        session.add(
            Variant(
                id="var_1",
                product_id="prod_1",
                sku="ML-TEE-1",
                size="M",
                color="Black",
                unit_price_minor=499_900,
                currency="INR",
                active=True,
            )
        )
        await session.flush()
        session.add(
            Inventory(
                variant_id="var_1",
                on_hand=10,
                reserved=0 if late_capture else 1,
                sold=0,
                version=2,
            )
        )
        session.add(
            CheckoutSession(
                id="chk_1",
                merchant_id="merchant_demo",
                buyer_key_id="buyer_1",
                status="expired" if late_capture else "payment_pending",
                version=3,
                policy_pack_version=1,
                pickup_location_id="pickup_blr_01",
                currency="INR",
                expires_at=NOW + timedelta(minutes=30),
            )
        )
        await session.flush()
        session.add(
            CheckoutLine(
                checkout_id="chk_1",
                position=1,
                variant_id="var_1",
                quantity=1,
                product_name="MerchantLatch Tee",
                sku="ML-TEE-1",
                size="M",
                color="Black",
                unit_price_minor=499_900,
                inventory_version=1,
            )
        )
        snapshot = ApprovalSnapshotRecord(
            checkout_id="chk_1",
            checkout_version=2,
            policy_pack_version=1,
            checksum="a" * 64,
            canonical_body=b"{}",
            approved_by="buyer_1",
            approved_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        session.add(snapshot)
        await session.flush()
        session.add(
            PaymentAttempt(
                id="att_1",
                checkout_id="chk_1",
                attempt_version=1,
                state="paid_inventory_exception" if late_capture else "awaiting_payment",
                receipt="acsa1_receipt",
                snapshot_id=snapshot.id,
                snapshot_checksum=snapshot.checksum,
                amount_minor=499_900,
                currency="INR",
                provider_uncertain=False,
                provider_account_id="rzp_test_account" if late_capture else None,
                provider_payment_id="pay_1" if late_capture else None,
                payment_evidence_digest="b" * 64 if late_capture else None,
                payment_evidence_source="webhook" if late_capture else None,
            )
        )
        await session.flush()
        session.add(
            InventoryLease(
                attempt_id="att_1",
                state="released" if late_capture else "active",
                expires_at=NOW - timedelta(seconds=1),
                released_at=NOW if late_capture else None,
            )
        )


class ProviderStub:
    def __init__(self, *, create_status: str = "processed") -> None:
        self.create_status = create_status
        self.create_calls = 0
        self.fetch_calls = 0

    async def create_full_refund(self, **kwargs: object) -> ProviderRefundRecord:
        self.create_calls += 1
        return ProviderRefundRecord(
            refund_id="rfnd_1",
            payment_id=str(kwargs["payment_id"]),
            amount_minor=int(kwargs["amount_minor"]),
            currency="INR",
            receipt=str(kwargs["receipt"]),
            status=self.create_status,
            notes=dict(kwargs["notes"]),  # type: ignore[arg-type]
        )

    async def fetch_refund(self, refund_id: str) -> ProviderRefundRecord:
        self.fetch_calls += 1
        return ProviderRefundRecord(
            refund_id=refund_id,
            payment_id="pay_1",
            amount_minor=499_900,
            currency="INR",
            receipt="",
            status="processed",
            notes={"attempt_id": "att_1", "reason": "inventory_exception"},
        )


async def test_expired_lease_releases_reserved_inventory_once(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_attempt(session_factory, late_capture=False)
    store = PostgresRefundStore(session_factory)

    first = await store.release_expired_leases(limit=100)
    second = await store.release_expired_leases(limit=100)

    assert (first, second) == (1, 0)
    async with session_factory() as session:
        inventory = await session.get(Inventory, "var_1")
        lease = await session.scalar(
            select(InventoryLease).where(InventoryLease.attempt_id == "att_1")
        )
        attempt = await session.get(PaymentAttempt, "att_1")
        checkout = await session.get(CheckoutSession, "chk_1")
        assert inventory is not None and inventory.reserved == 0
        assert lease is not None and lease.state == "expired"
        assert attempt is not None and attempt.state == "expired"
        assert checkout is not None and checkout.status == "expired"


async def test_late_capture_creates_and_persists_exactly_one_full_refund(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_attempt(session_factory, late_capture=True)
    provider = ProviderStub()
    service = RefundService(
        store=PostgresRefundStore(session_factory),
        provider=provider,
    )

    first = await service.process("att_1")
    second = await service.process("att_1")

    assert (first, second) == (RefundOutcome.REFUNDED, RefundOutcome.IGNORED)
    assert provider.create_calls == 1
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, "att_1")
        refund = await session.scalar(select(Refund).where(Refund.attempt_id == "att_1"))
        assert attempt is not None and attempt.state == "refunded"
        assert refund is not None and refund.amount_minor == 499_900
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1


async def test_pending_refund_is_fetched_without_second_create(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_attempt(session_factory, late_capture=True)
    provider = ProviderStub(create_status="pending")
    service = RefundService(
        store=PostgresRefundStore(session_factory),
        provider=provider,
    )

    first = await service.process("att_1")
    async with session_factory() as session:
        refund = await session.scalar(select(Refund).where(Refund.attempt_id == "att_1"))
        assert refund is not None
        provider_receipt = refund.receipt
    original_fetch = provider.fetch_refund

    async def fetch_with_receipt(refund_id: str) -> ProviderRefundRecord:
        result = await original_fetch(refund_id)
        return ProviderRefundRecord(
            refund_id=result.refund_id,
            payment_id=result.payment_id,
            amount_minor=result.amount_minor,
            currency=result.currency,
            receipt=provider_receipt,
            status=result.status,
            notes=result.notes,
        )

    provider.fetch_refund = fetch_with_receipt  # type: ignore[method-assign]
    second = await service.process("att_1")

    assert (first, second) == (RefundOutcome.PENDING, RefundOutcome.REFUNDED)
    assert provider.create_calls == 1
    assert provider.fetch_calls == 1


async def test_verified_refund_webhook_finds_attempt_after_an_uncertain_create(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_attempt(session_factory, late_capture=True)
    async with session_factory() as session, session.begin():
        event = WebhookEvent(
            event_id="evt_refund_1",
            event_name="refund.processed",
            payload_hash="c" * 64,
            payment_id="pay_1",
            refund_id="rfnd_1",
        )
        session.add(event)
        await session.flush()
        event_id = event.id

    work = await PostgresWebhookStore(session_factory).load_refund_work(event_id)

    assert work is not None
    assert work.attempt_id == "att_1"
    assert work.refund_id == "rfnd_1"
