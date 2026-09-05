from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from acsa.adapters.razorpay.client import RazorpayProviderError
from acsa.domain.receipts import ProviderOrderCandidate
from acsa.services.payment_orders import (
    PaymentOrderAction,
    PaymentOrderOutcome,
    PaymentOrderService,
    PaymentOrderWork,
)


def _candidate(order_id: str = "order_exact") -> ProviderOrderCandidate:
    return ProviderOrderCandidate(
        order_id=order_id,
        receipt="acsa1_receipt",
        amount_minor=499_900,
        currency="INR",
        notes={
            "checkout_id": "chk_1",
            "attempt_id": "att_1",
            "snapshot_checksum": "a" * 64,
        },
    )


def _work(action: PaymentOrderAction) -> PaymentOrderWork:
    return PaymentOrderWork(
        action=action,
        attempt_id="att_1",
        checkout_id="chk_1",
        merchant_id="merchant_demo",
        receipt="acsa1_receipt",
        amount_minor=499_900,
        currency="INR",
        snapshot_checksum="a" * 64,
    )


@dataclass
class MemoryStore:
    work: PaymentOrderWork
    stored: list[tuple[ProviderOrderCandidate, bool]] = field(default_factory=list)
    reconciling: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    async def prepare(self, attempt_id: str) -> PaymentOrderWork:
        assert attempt_id == self.work.attempt_id
        return self.work

    async def store_order(
        self, attempt_id: str, order: ProviderOrderCandidate, *, recovered: bool
    ) -> bool:
        assert attempt_id == self.work.attempt_id
        self.stored.append((order, recovered))
        return True

    async def mark_reconciling(self, attempt_id: str) -> None:
        self.reconciling.append(attempt_id)

    async def mark_failed(self, attempt_id: str) -> None:
        self.failed.append(attempt_id)


@dataclass
class ProviderStub:
    create_result: ProviderOrderCandidate | BaseException
    search_results: list[ProviderOrderCandidate] = field(default_factory=list)
    create_calls: int = 0
    search_calls: int = 0

    async def create_order(self, **_: object) -> ProviderOrderCandidate:
        self.create_calls += 1
        if isinstance(self.create_result, BaseException):
            raise self.create_result
        return self.create_result

    async def fetch_orders_by_receipt(self, receipt: str) -> list[ProviderOrderCandidate]:
        assert receipt == "acsa1_receipt"
        self.search_calls += 1
        return self.search_results


@pytest.mark.asyncio
async def test_normal_path_creates_once_and_persists_only_an_exact_order() -> None:
    store = MemoryStore(_work(PaymentOrderAction.CREATE))
    provider = ProviderStub(_candidate())

    result = await PaymentOrderService(store=store, provider=provider).process("att_1")

    assert result is PaymentOrderOutcome.CREATED
    assert provider.create_calls == 1
    assert provider.search_calls == 0
    assert store.stored == [(_candidate(), False)]


@pytest.mark.asyncio
async def test_uncertain_create_moves_to_reconciliation_without_replacement_create() -> None:
    store = MemoryStore(_work(PaymentOrderAction.CREATE))
    provider = ProviderStub(httpx.ReadTimeout("response lost"))
    service = PaymentOrderService(store=store, provider=provider)

    first = await service.process("att_1")
    store.work = _work(PaymentOrderAction.RECOVER)
    provider.search_results = [_candidate()]
    second = await service.process("att_1")

    assert first is PaymentOrderOutcome.RECONCILING
    assert second is PaymentOrderOutcome.RECOVERED
    assert provider.create_calls == 1
    assert provider.search_calls == 1
    assert store.stored == [(_candidate(), True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RazorpayProviderError(status_code=None, operation="create_order"),
        RazorpayProviderError(status_code=503, operation="create_order"),
    ],
)
async def test_malformed_or_server_error_create_response_is_reconciled(
    error: RazorpayProviderError,
) -> None:
    store = MemoryStore(_work(PaymentOrderAction.CREATE))
    provider = ProviderStub(error)

    result = await PaymentOrderService(store=store, provider=provider).process("att_1")

    assert result is PaymentOrderOutcome.RECONCILING
    assert provider.create_calls == 1
    assert store.reconciling == ["att_1"]
    assert store.failed == []


@pytest.mark.asyncio
async def test_client_error_create_response_fails_without_reconciliation() -> None:
    store = MemoryStore(_work(PaymentOrderAction.CREATE))
    provider = ProviderStub(RazorpayProviderError(status_code=400, operation="create_order"))

    result = await PaymentOrderService(store=store, provider=provider).process("att_1")

    assert result is PaymentOrderOutcome.FAILED
    assert provider.create_calls == 1
    assert store.failed == ["att_1"]
    assert store.reconciling == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidates",
    [
        [],
        [_candidate("order_1"), _candidate("order_2")],
        [
            ProviderOrderCandidate(
                order_id="order_wrong",
                receipt="acsa1_receipt",
                amount_minor=1,
                currency="INR",
                notes={},
            )
        ],
    ],
)
async def test_reconciliation_never_creates_or_accepts_zero_ambiguous_or_conflicting_matches(
    candidates: list[ProviderOrderCandidate],
) -> None:
    store = MemoryStore(_work(PaymentOrderAction.RECOVER))
    provider = ProviderStub(_candidate(), search_results=candidates)

    result = await PaymentOrderService(store=store, provider=provider).process("att_1")

    assert result is PaymentOrderOutcome.RECONCILING
    assert provider.create_calls == 0
    assert provider.search_calls == 1
    assert store.stored == []
    assert store.reconciling == ["att_1"]
