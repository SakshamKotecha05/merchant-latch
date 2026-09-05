from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from acsa.domain.payments import ProviderRefundRecord
from acsa.services.refunds import (
    RefundAction,
    RefundOutcome,
    RefundService,
    RefundWork,
)


def _work(action: RefundAction = RefundAction.CREATE) -> RefundWork:
    return RefundWork(
        action=action,
        attempt_id="att_1",
        payment_id="pay_1",
        amount_minor=499_900,
        currency="INR",
        receipt="acsarfnd1_fixture",
        provider_refund_id="refund_1" if action is RefundAction.FETCH else None,
    )


def _refund(*, status: str = "processed", amount_minor: int = 499_900) -> ProviderRefundRecord:
    return ProviderRefundRecord(
        refund_id="refund_1",
        payment_id="pay_1",
        amount_minor=amount_minor,
        currency="INR",
        receipt="acsarfnd1_fixture",
        status=status,
        notes={"attempt_id": "att_1", "reason": "inventory_exception"},
    )


@dataclass
class StoreStub:
    work: RefundWork = field(default_factory=_work)
    stored: list[ProviderRefundRecord] = field(default_factory=list)
    manual_review: list[str] = field(default_factory=list)
    released_leases: int = 0

    async def prepare(self, attempt_id: str) -> RefundWork:
        assert attempt_id == "att_1"
        return self.work

    async def store_refund(self, attempt_id: str, refund: ProviderRefundRecord) -> RefundOutcome:
        self.stored.append(refund)
        return RefundOutcome.REFUNDED if refund.status == "processed" else RefundOutcome.PENDING

    async def mark_manual_review(self, attempt_id: str) -> None:
        self.manual_review.append(attempt_id)

    async def release_expired_leases(self, *, limit: int) -> int:
        assert limit == 100
        return self.released_leases


@dataclass
class ProviderStub:
    create_result: ProviderRefundRecord | BaseException = field(default_factory=_refund)
    fetch_result: ProviderRefundRecord | BaseException = field(default_factory=_refund)
    creates: list[dict[str, object]] = field(default_factory=list)
    fetches: list[str] = field(default_factory=list)

    async def create_full_refund(self, **kwargs: object) -> ProviderRefundRecord:
        self.creates.append(kwargs)
        if isinstance(self.create_result, BaseException):
            raise self.create_result
        return self.create_result

    async def fetch_refund(self, refund_id: str) -> ProviderRefundRecord:
        self.fetches.append(refund_id)
        if isinstance(self.fetch_result, BaseException):
            raise self.fetch_result
        return self.fetch_result


@pytest.mark.asyncio
async def test_late_capture_creates_one_full_idempotent_refund() -> None:
    store = StoreStub()
    provider = ProviderStub()

    outcome = await RefundService(store=store, provider=provider).process("att_1")

    assert outcome is RefundOutcome.REFUNDED
    assert provider.creates == [
        {
            "payment_id": "pay_1",
            "amount_minor": 499_900,
            "receipt": "acsarfnd1_fixture",
            "notes": {"attempt_id": "att_1", "reason": "inventory_exception"},
        }
    ]
    assert provider.fetches == []
    assert store.stored == [_refund()]


@pytest.mark.asyncio
async def test_duplicate_completed_job_never_creates_a_second_refund() -> None:
    store = StoreStub(work=_work(RefundAction.COMPLETE))
    provider = ProviderStub()

    outcome = await RefundService(store=store, provider=provider).process("att_1")

    assert outcome is RefundOutcome.IGNORED
    assert provider.creates == []
    assert provider.fetches == []


@pytest.mark.asyncio
async def test_uncertain_create_is_retryable_with_the_same_receipt() -> None:
    store = StoreStub()
    provider = ProviderStub(create_result=httpx.ReadTimeout("response lost"))

    outcome = await RefundService(store=store, provider=provider).process("att_1")

    assert outcome is RefundOutcome.RETRY
    assert provider.creates[0]["receipt"] == "acsarfnd1_fixture"
    assert store.stored == []
    assert store.manual_review == []


@pytest.mark.asyncio
async def test_pending_refund_is_fetched_without_another_create() -> None:
    store = StoreStub(work=_work(RefundAction.FETCH))
    provider = ProviderStub(fetch_result=_refund(status="processed"))

    outcome = await RefundService(store=store, provider=provider).process("att_1")

    assert outcome is RefundOutcome.REFUNDED
    assert provider.creates == []
    assert provider.fetches == ["refund_1"]


@pytest.mark.asyncio
async def test_verified_refund_webhook_fetches_the_named_refund_without_creating() -> None:
    store = StoreStub()
    provider = ProviderStub(fetch_result=_refund(status="processed"))

    outcome = await RefundService(store=store, provider=provider).reconcile_webhook(
        "att_1", "refund_1"
    )

    assert outcome is RefundOutcome.REFUNDED
    assert provider.creates == []
    assert provider.fetches == ["refund_1"]
    assert store.stored == [_refund()]


@pytest.mark.asyncio
async def test_conflicting_refund_evidence_moves_to_manual_review() -> None:
    store = StoreStub()
    provider = ProviderStub(create_result=_refund(amount_minor=1))

    outcome = await RefundService(store=store, provider=provider).process("att_1")

    assert outcome is RefundOutcome.MANUAL_REVIEW
    assert store.stored == []
    assert store.manual_review == ["att_1"]


@pytest.mark.asyncio
async def test_lease_expiry_service_runs_a_bounded_database_sweep() -> None:
    store = StoreStub(released_leases=3)

    released = await RefundService(store=store, provider=ProviderStub()).release_expired_leases(
        limit=100
    )

    assert released == 3
