"""Create or recover exactly one provider Order per payment attempt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from acsa.adapters.razorpay.client import RazorpayProviderError
from acsa.domain.receipts import ProviderOrderCandidate, select_exact_order


class PaymentOrderAction(StrEnum):
    CREATE = "create"
    RECOVER = "recover"
    COMPLETE = "complete"
    NOT_FOUND = "not_found"


class PaymentOrderOutcome(StrEnum):
    CREATED = "created"
    RECOVERED = "recovered"
    RECONCILING = "reconciling"
    FAILED = "failed"
    IGNORED = "ignored"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class PaymentOrderWork:
    action: PaymentOrderAction
    attempt_id: str
    checkout_id: str
    merchant_id: str
    receipt: str
    amount_minor: int
    currency: str
    snapshot_checksum: str

    @property
    def notes(self) -> Mapping[str, str]:
        return {
            "checkout_id": self.checkout_id,
            "attempt_id": self.attempt_id,
            "snapshot_checksum": self.snapshot_checksum,
        }


class PaymentOrderStorePort(Protocol):
    async def prepare(self, attempt_id: str) -> PaymentOrderWork: ...

    async def store_order(
        self,
        attempt_id: str,
        order: ProviderOrderCandidate,
        *,
        recovered: bool,
    ) -> bool: ...

    async def mark_reconciling(self, attempt_id: str) -> None: ...

    async def mark_failed(self, attempt_id: str) -> None: ...


class ProviderOrderPort(Protocol):
    async def create_order(
        self,
        *,
        amount_minor: int,
        currency: str,
        receipt: str,
        notes: Mapping[str, str],
    ) -> ProviderOrderCandidate: ...

    async def fetch_orders_by_receipt(self, receipt: str) -> list[ProviderOrderCandidate]: ...


class PaymentOrderService:
    def __init__(self, *, store: PaymentOrderStorePort, provider: ProviderOrderPort) -> None:
        self._store = store
        self._provider = provider

    async def process(self, attempt_id: str) -> PaymentOrderOutcome:
        work = await self._store.prepare(attempt_id)
        if work.action is PaymentOrderAction.NOT_FOUND:
            return PaymentOrderOutcome.NOT_FOUND
        if work.action is PaymentOrderAction.COMPLETE:
            return PaymentOrderOutcome.IGNORED
        if work.action is PaymentOrderAction.RECOVER:
            return await self._recover(work)
        try:
            order = await self._provider.create_order(
                amount_minor=work.amount_minor,
                currency=work.currency,
                receipt=work.receipt,
                notes=work.notes,
            )
        except httpx.TimeoutException:
            await self._store.mark_reconciling(work.attempt_id)
            return PaymentOrderOutcome.RECONCILING
        except RazorpayProviderError as error:
            if error.status_code is not None and error.status_code < 500:
                await self._store.mark_failed(work.attempt_id)
                return PaymentOrderOutcome.FAILED
            await self._store.mark_reconciling(work.attempt_id)
            return PaymentOrderOutcome.RECONCILING
        exact = select_exact_order(
            [order],
            expected_receipt=work.receipt,
            expected_amount_minor=work.amount_minor,
            expected_currency=work.currency,
            expected_notes=work.notes,
        )
        if exact is None:
            await self._store.mark_reconciling(work.attempt_id)
            return PaymentOrderOutcome.RECONCILING
        stored = await self._store.store_order(work.attempt_id, exact, recovered=False)
        return PaymentOrderOutcome.CREATED if stored else PaymentOrderOutcome.IGNORED

    async def _recover(self, work: PaymentOrderWork) -> PaymentOrderOutcome:
        try:
            candidates = await self._provider.fetch_orders_by_receipt(work.receipt)
        except (httpx.TimeoutException, RazorpayProviderError):
            await self._store.mark_reconciling(work.attempt_id)
            return PaymentOrderOutcome.RECONCILING
        exact = select_exact_order(
            candidates,
            expected_receipt=work.receipt,
            expected_amount_minor=work.amount_minor,
            expected_currency=work.currency,
            expected_notes=work.notes,
        )
        if exact is None:
            await self._store.mark_reconciling(work.attempt_id)
            return PaymentOrderOutcome.RECONCILING
        stored = await self._store.store_order(work.attempt_id, exact, recovered=True)
        return PaymentOrderOutcome.RECOVERED if stored else PaymentOrderOutcome.IGNORED
