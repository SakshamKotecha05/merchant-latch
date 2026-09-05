from __future__ import annotations

import asyncio
import hashlib
import hmac
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
    MerchantOrder,
    OutboxJob,
    PaymentAttempt,
    PickupLocation,
    Product,
    ProviderOrder,
    Variant,
    WebhookEvent,
)
from acsa.adapters.postgres.payment_finalization import PostgresPaymentFinalizationStore
from acsa.adapters.postgres.refunds import PostgresRefundStore
from acsa.adapters.postgres.webhooks import PostgresWebhookStore
from acsa.domain.payments import ProviderOrderRecord, ProviderPaymentRecord
from acsa.services.payment_finalization import (
    EvidenceSource,
    FinalizationOutcome,
    PaymentFinalizationService,
)

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC)


class ProviderStub:
    async def fetch_payment(self, payment_id: str) -> ProviderPaymentRecord:
        await asyncio.sleep(0)
        return ProviderPaymentRecord(
            payment_id=payment_id,
            order_id="order_1",
            amount_minor=499_900,
            currency="INR",
            status="captured",
            captured=True,
        )

    async def fetch_order(self, order_id: str) -> ProviderOrderRecord:
        await asyncio.sleep(0)
        return ProviderOrderRecord(
            order_id=order_id,
            receipt="acsa1_receipt",
            amount_minor=499_900,
            currency="INR",
            status="paid",
            notes={
                "checkout_id": "chk_1",
                "attempt_id": "att_1",
                "snapshot_checksum": "a" * 64,
            },
        )


async def _seed_finalizable_attempt(session_factory, *, lease_active: bool = True) -> None:  # type: ignore[no-untyped-def]
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
                reserved=1 if lease_active else 0,
                sold=0,
                version=2,
            )
        )
        session.add(
            CheckoutSession(
                id="chk_1",
                merchant_id="merchant_demo",
                buyer_key_id="buyer_1",
                status="payment_pending",
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
                state="awaiting_payment",
                receipt="acsa1_receipt",
                snapshot_id=snapshot.id,
                snapshot_checksum=snapshot.checksum,
                amount_minor=499_900,
                currency="INR",
                provider_uncertain=False,
            )
        )
        await session.flush()
        session.add(
            ProviderOrder(
                attempt_id="att_1",
                provider_order_id="order_1",
                receipt="acsa1_receipt",
                amount_minor=499_900,
                currency="INR",
                notes={
                    "checkout_id": "chk_1",
                    "attempt_id": "att_1",
                    "snapshot_checksum": "a" * 64,
                },
                recovered=False,
            )
        )
        session.add(
            InventoryLease(
                attempt_id="att_1",
                state="active" if lease_active else "released",
                expires_at=NOW + timedelta(minutes=10),
                released_at=None if lease_active else NOW,
            )
        )


def _service(session_factory) -> PaymentFinalizationService:  # type: ignore[no-untyped-def]
    return PaymentFinalizationService(
        store=PostgresPaymentFinalizationStore(session_factory),
        provider=ProviderStub(),
        provider_account_id="rzp_test_account",
        checkout_secret="fixture-secret",
    )


async def test_concurrent_browser_and_webhook_evidence_finalize_exactly_once(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_finalizable_attempt(session_factory)
    signature = hmac.new(
        b"fixture-secret",
        b"order_1|pay_1",
        hashlib.sha256,
    ).hexdigest()
    browser = EvidenceSource.browser(payment_id="pay_1", signature=signature)
    webhook = EvidenceSource.webhook(payment_id="pay_1", order_id="order_1")
    service = _service(session_factory)

    outcomes = await asyncio.gather(
        service.finalize_payment("att_1", browser),
        service.finalize_payment("att_1", webhook),
    )

    assert sorted(outcomes) == sorted([FinalizationOutcome.COMPLETED, FinalizationOutcome.REPLAYED])
    async with session_factory() as session:
        checkout = await session.get(CheckoutSession, "chk_1")
        attempt = await session.get(PaymentAttempt, "att_1")
        inventory = await session.get(Inventory, "var_1")
        lease = await session.scalar(
            select(InventoryLease).where(InventoryLease.attempt_id == "att_1")
        )
        assert checkout is not None and checkout.status == "completed"
        assert attempt is not None and attempt.state == "paid"
        assert inventory is not None and (inventory.reserved, inventory.sold) == (0, 1)
        assert lease is not None and lease.state == "consumed"
        assert await session.scalar(select(func.count()).select_from(MerchantOrder)) == 1
        jobs = list(
            await session.scalars(
                select(OutboxJob.job_type).where(OutboxJob.aggregate_id == "att_1")
            )
        )
        assert jobs == []


async def test_released_lease_routes_captured_payment_to_inventory_exception(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_finalizable_attempt(session_factory, lease_active=False)

    outcome = await _service(session_factory).finalize_payment(
        "att_1",
        EvidenceSource.webhook(payment_id="pay_1", order_id="order_1"),
    )

    assert outcome is FinalizationOutcome.INVENTORY_EXCEPTION
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, "att_1")
        assert attempt is not None and attempt.state == "paid_inventory_exception"
        assert await session.scalar(select(func.count()).select_from(MerchantOrder)) == 0
        refund_jobs = await session.scalar(
            select(func.count())
            .select_from(OutboxJob)
            .where(OutboxJob.job_type == "refund_captured_payment")
        )
        assert refund_jobs == 1


async def test_time_expired_lease_is_released_before_refund_is_enqueued(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_finalizable_attempt(session_factory)
    async with session_factory() as session, session.begin():
        lease = await session.scalar(
            select(InventoryLease).where(InventoryLease.attempt_id == "att_1")
        )
        assert lease is not None
        lease.expires_at = NOW - timedelta(seconds=1)

    outcome = await _service(session_factory).finalize_payment(
        "att_1",
        EvidenceSource.webhook(payment_id="pay_1", order_id="order_1"),
    )

    assert outcome is FinalizationOutcome.INVENTORY_EXCEPTION
    async with session_factory() as session:
        lease = await session.scalar(
            select(InventoryLease).where(InventoryLease.attempt_id == "att_1")
        )
        inventory = await session.get(Inventory, "var_1")
        assert lease is not None and lease.state == "expired"
        assert inventory is not None and inventory.reserved == 0


async def test_completed_checkout_replays_without_repeating_effects(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_finalizable_attempt(session_factory)
    async with session_factory() as session, session.begin():
        checkout = await session.get(CheckoutSession, "chk_1")
        assert checkout is not None
        checkout.status = "completed"

    outcome = await _service(session_factory).finalize_payment(
        "att_1",
        EvidenceSource.webhook(payment_id="pay_1", order_id="order_1"),
    )

    assert outcome is FinalizationOutcome.REPLAYED
    async with session_factory() as session:
        inventory = await session.get(Inventory, "var_1")
        assert inventory is not None and (inventory.reserved, inventory.sold) == (1, 0)
        assert await session.scalar(select(func.count()).select_from(MerchantOrder)) == 0


async def test_inventory_mismatch_rolls_back_every_variant_mutation(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_finalizable_attempt(session_factory)
    async with session_factory() as session, session.begin():
        session.add(
            Variant(
                id="var_2",
                product_id="prod_1",
                sku="ML-TEE-2",
                size="L",
                color="Black",
                unit_price_minor=100,
                currency="INR",
                active=True,
            )
        )
        await session.flush()
        session.add(Inventory(variant_id="var_2", on_hand=10, reserved=0, sold=0, version=1))
        session.add(
            CheckoutLine(
                checkout_id="chk_1",
                position=2,
                variant_id="var_2",
                quantity=1,
                product_name="MerchantLatch Tee",
                sku="ML-TEE-2",
                size="L",
                color="Black",
                unit_price_minor=100,
                inventory_version=1,
            )
        )

    outcome = await _service(session_factory).finalize_payment(
        "att_1",
        EvidenceSource.webhook(payment_id="pay_1", order_id="order_1"),
    )

    assert outcome is FinalizationOutcome.RECONCILING
    async with session_factory() as session:
        inventory = list(await session.scalars(select(Inventory).order_by(Inventory.variant_id)))
        lease = await session.scalar(
            select(InventoryLease).where(InventoryLease.attempt_id == "att_1")
        )
        assert [(row.reserved, row.sold) for row in inventory] == [(1, 0), (0, 0)]
        assert lease is not None and lease.state == "active"


async def test_capture_after_expiry_enters_refund_path_instead_of_replaying(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_finalizable_attempt(session_factory)
    async with session_factory() as session, session.begin():
        lease = await session.scalar(
            select(InventoryLease).where(InventoryLease.attempt_id == "att_1")
        )
        assert lease is not None
        lease.expires_at = NOW - timedelta(seconds=1)
    assert await PostgresRefundStore(session_factory).release_expired_leases(limit=100) == 1

    outcome = await _service(session_factory).finalize_payment(
        "att_1",
        EvidenceSource.webhook(payment_id="pay_1", order_id="order_1"),
    )

    assert outcome is FinalizationOutcome.INVENTORY_EXCEPTION
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, "att_1")
        refund_jobs = await session.scalar(
            select(func.count())
            .select_from(OutboxJob)
            .where(OutboxJob.job_type == "refund_captured_payment")
        )
        assert attempt is not None and attempt.state == "paid_inventory_exception"
        assert refund_jobs == 1


async def test_captured_payment_webhook_maps_to_the_stored_attempt(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_finalizable_attempt(session_factory)
    raw_payload = (
        b'{"event":"payment.captured","payload":{"payment":{"entity":'
        b'{"id":"pay_1","order_id":"order_1"}}}}'
    )
    store = PostgresWebhookStore(session_factory)
    inserted = await store.insert_verified_event(
        event_id="evt_1",
        event_name="payment.captured",
        raw_payload=raw_payload,
        payload_hash=hashlib.sha256(raw_payload).hexdigest(),
    )
    async with session_factory() as session:
        event_id = await session.scalar(
            select(WebhookEvent.id).where(WebhookEvent.event_id == "evt_1")
        )

    assert inserted.created is True
    assert event_id is not None
    work = await store.load_finalization_work(event_id)
    assert work is not None
    assert (work.attempt_id, work.payment_id, work.order_id) == (
        "att_1",
        "pay_1",
        "order_1",
    )


async def test_non_finalizing_webhook_stays_visible_without_triggering_payment(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_finalizable_attempt(session_factory)
    raw_payload = (
        b'{"event":"payment.failed","payload":{"payment":{"entity":'
        b'{"id":"pay_1","order_id":"order_1"}}}}'
    )
    store = PostgresWebhookStore(session_factory)
    await store.insert_verified_event(
        event_id="evt_failed",
        event_name="payment.failed",
        raw_payload=raw_payload,
        payload_hash=hashlib.sha256(raw_payload).hexdigest(),
    )
    async with session_factory() as session:
        event_id = await session.scalar(
            select(WebhookEvent.id).where(WebhookEvent.event_id == "evt_failed")
        )

    assert event_id is not None
    assert await store.load_finalization_work(event_id) is None


async def test_completed_ucp_projection_has_minimal_order_confirmation(session_factory):
    import orjson

    from acsa.adapters.postgres.browser_sessions import PostgresBrowserSessionStore
    from acsa.adapters.postgres.commerce import PostgresCommerceStore
    from acsa.adapters.postgres.models import UCPCheckout

    await _seed_finalizable_attempt(session_factory)
    async with session_factory() as session, session.begin():
        session.add(
            UCPCheckout(
                id="chk_1",
                buyer_key_id="buyer_1",
                status="requires_escalation",
                resource={},
                continue_url="https://merchant.example/checkout/chk_1?session=secret",
                expires_at=NOW + timedelta(minutes=30),
                response_body=b"{}",
            )
        )
    result = await _service(session_factory).finalize_payment(
        "att_1", EvidenceSource.webhook(payment_id="pay_1", order_id="order_1")
    )
    assert result is FinalizationOutcome.COMPLETED
    checkout = await PostgresCommerceStore(session_factory).get_checkout(
        "chk_1", buyer_key_id="buyer_1"
    )
    assert checkout is not None
    resource = orjson.loads(checkout.canonical_bytes)
    assert resource["status"] == "completed"
    assert "continue_url" not in resource
    assert (
        resource["order"]["permalink_url"]
        == "https://merchant.example/orders/" + resource["order"]["id"]
    )
    from uuid import UUID

    confirmation = await PostgresBrowserSessionStore(session_factory).public_order(
        UUID(resource["order"]["id"])
    )
    assert set(confirmation) == {"id", "status", "amount", "currency"}
    assert confirmation["amount"] == 499900
