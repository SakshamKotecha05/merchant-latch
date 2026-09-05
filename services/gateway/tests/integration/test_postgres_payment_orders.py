from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from acsa.adapters.postgres.models import (
    ApprovalSnapshotRecord,
    CheckoutSession,
    MerchantConfig,
    PaymentAttempt,
    PickupLocation,
    ProviderOrder,
)
from acsa.adapters.postgres.payment_orders import PostgresPaymentOrderStore
from acsa.domain.receipts import ProviderOrderCandidate
from acsa.services.payment_orders import PaymentOrderOutcome, PaymentOrderService

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


async def _seed_attempt(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session, session.begin():
        merchant = MerchantConfig(
            id="merchant_demo",
            public_name="MerchantLatch",
            currency="INR",
            active_policy_pack_version=1,
        )
        session.add(merchant)
        await session.flush()
        pickup = PickupLocation(
            id="pickup_blr_01",
            merchant_id="merchant_demo",
            name="MerchantLatch Bengaluru",
            city="Bengaluru",
            active=True,
        )
        session.add(pickup)
        await session.flush()
        session.add(
            CheckoutSession(
                id="chk_1",
                merchant_id="merchant_demo",
                buyer_key_id="buyer_1",
                status="approved",
                version=2,
                policy_pack_version=1,
                pickup_location_id="pickup_blr_01",
                currency="INR",
                expires_at=NOW + timedelta(minutes=30),
            )
        )
        await session.flush()
        snapshot = ApprovalSnapshotRecord(
            checkout_id="chk_1",
            checkout_version=1,
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
                state="draft",
                receipt="acsa1_receipt",
                snapshot_id=snapshot.id,
                snapshot_checksum=snapshot.checksum,
                amount_minor=499_900,
                currency="INR",
                provider_uncertain=False,
            )
        )


class ProviderStub:
    def __init__(
        self,
        *,
        create_result: ProviderOrderCandidate | BaseException,
        search_results: list[ProviderOrderCandidate] | None = None,
    ) -> None:
        self.create_result = create_result
        self.search_results = search_results or []
        self.create_calls = 0
        self.search_calls = 0

    async def create_order(self, **_: object) -> ProviderOrderCandidate:
        self.create_calls += 1
        if isinstance(self.create_result, BaseException):
            raise self.create_result
        return self.create_result

    async def fetch_orders_by_receipt(self, _: str) -> list[ProviderOrderCandidate]:
        self.search_calls += 1
        return self.search_results


def _exact_order() -> ProviderOrderCandidate:
    return ProviderOrderCandidate(
        order_id="order_1",
        receipt="acsa1_receipt",
        amount_minor=499_900,
        currency="INR",
        notes={
            "checkout_id": "chk_1",
            "attempt_id": "att_1",
            "snapshot_checksum": "a" * 64,
        },
    )


async def test_normal_provider_order_is_persisted_before_payment_waits(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_attempt(session_factory)
    provider = ProviderStub(create_result=_exact_order())
    service = PaymentOrderService(
        store=PostgresPaymentOrderStore(session_factory),
        provider=provider,
    )

    result = await service.process("att_1")

    assert result is PaymentOrderOutcome.CREATED
    assert provider.create_calls == 1
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, "att_1")
        checkout = await session.get(CheckoutSession, "chk_1")
        assert attempt is not None and attempt.state == "awaiting_payment"
        assert checkout is not None and checkout.status == "payment_pending"
        assert await session.scalar(select(func.count()).select_from(ProviderOrder)) == 1


async def test_lost_create_response_recovers_exact_order_without_second_create(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_attempt(session_factory)
    store = PostgresPaymentOrderStore(session_factory)
    uncertain = ProviderStub(create_result=httpx.ReadTimeout("response lost"))
    first = await PaymentOrderService(store=store, provider=uncertain).process("att_1")
    recovery = ProviderStub(create_result=_exact_order(), search_results=[_exact_order()])

    second = await PaymentOrderService(store=store, provider=recovery).process("att_1")

    assert first is PaymentOrderOutcome.RECONCILING
    assert second is PaymentOrderOutcome.RECOVERED
    assert uncertain.create_calls == 1
    assert recovery.create_calls == 0
    assert recovery.search_calls == 1


async def test_reconciliation_miss_keeps_attempt_uncertain_without_provider_row(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_attempt(session_factory)
    store = PostgresPaymentOrderStore(session_factory)
    timeout_provider = ProviderStub(create_result=httpx.ReadTimeout("response lost"))
    await PaymentOrderService(store=store, provider=timeout_provider).process("att_1")
    provider = ProviderStub(create_result=_exact_order(), search_results=[])

    result = await PaymentOrderService(store=store, provider=provider).process("att_1")

    assert result is PaymentOrderOutcome.RECONCILING
    assert provider.create_calls == 0
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, "att_1")
        assert attempt is not None and attempt.state == "reconciling"
        assert attempt.provider_uncertain is True
        assert await session.scalar(select(func.count()).select_from(ProviderOrder)) == 0
