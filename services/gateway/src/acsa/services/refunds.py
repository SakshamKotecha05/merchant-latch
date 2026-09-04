"""Idempotent full-refund processing for captured payments without inventory."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from acsa.adapters.razorpay.client import RazorpayProviderError
from acsa.domain.payments import ProviderRefundRecord


class RefundAction(StrEnum):
    CREATE = "create"
    FETCH = "fetch"
    COMPLETE = "complete"
    NOT_FOUND = "not_found"


class RefundOutcome(StrEnum):
    REFUNDED = "refunded"
    PENDING = "pending"
    RETRY = "retry"
    MANUAL_REVIEW = "manual_review"
    IGNORED = "ignored"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class RefundWork:
    action: RefundAction
    attempt_id: str
    payment_id: str
    amount_minor: int
    currency: str
    receipt: str
    provider_refund_id: str | None = None

    @property
    def notes(self) -> Mapping[str, str]:
        return {"attempt_id": self.attempt_id, "reason": "inventory_exception"}


class RefundStorePort(Protocol):
    async def prepare(self, attempt_id: str) -> RefundWork: ...

    async def store_refund(
        self, attempt_id: str, refund: ProviderRefundRecord
    ) -> RefundOutcome: ...

    async def mark_manual_review(self, attempt_id: str) -> None: ...

    async def release_expired_leases(self, *, limit: int) -> int: ...


class RefundProviderPort(Protocol):
    async def create_full_refund(
        self,
        *,
        payment_id: str,
        amount_minor: int,
        receipt: str,
        notes: Mapping[str, str],
    ) -> ProviderRefundRecord: ...

    async def fetch_refund(self, refund_id: str) -> ProviderRefundRecord: ...


class RefundService:
    def __init__(self, *, store: RefundStorePort, provider: RefundProviderPort) -> None:
        self._store = store
        self._provider = provider

    async def release_expired_leases(self, *, limit: int) -> int:
        return await self._store.release_expired_leases(limit=limit)

    async def process(self, attempt_id: str) -> RefundOutcome:
        work = await self._store.prepare(attempt_id)
        if work.action is RefundAction.NOT_FOUND:
            return RefundOutcome.NOT_FOUND
        if work.action is RefundAction.COMPLETE:
            return RefundOutcome.IGNORED
        try:
            if work.action is RefundAction.FETCH:
                if work.provider_refund_id is None:
                    return await self._manual_review(work.attempt_id)
                refund = await self._provider.fetch_refund(work.provider_refund_id)
            else:
                refund = await self._provider.create_full_refund(
                    payment_id=work.payment_id,
                    amount_minor=work.amount_minor,
                    receipt=work.receipt,
                    notes=work.notes,
                )
        except httpx.TimeoutException:
            return RefundOutcome.RETRY
        except RazorpayProviderError as error:
            if error.status_code is None or error.status_code >= 500:
                return RefundOutcome.RETRY
            return await self._manual_review(work.attempt_id)
        if not _refund_matches(work, refund) or refund.status not in {"pending", "processed"}:
            return await self._manual_review(work.attempt_id)
        return await self._store.store_refund(work.attempt_id, refund)

    async def reconcile_webhook(self, attempt_id: str, provider_refund_id: str) -> RefundOutcome:
        work = await self._store.prepare(attempt_id)
        if work.action is RefundAction.NOT_FOUND:
            return RefundOutcome.NOT_FOUND
        if work.action is RefundAction.COMPLETE:
            return RefundOutcome.IGNORED
        if work.provider_refund_id not in {None, provider_refund_id}:
            return await self._manual_review(work.attempt_id)
        try:
            refund = await self._provider.fetch_refund(provider_refund_id)
        except httpx.TimeoutException:
            return RefundOutcome.RETRY
        except RazorpayProviderError as error:
            if error.status_code is None or error.status_code >= 500:
                return RefundOutcome.RETRY
            return await self._manual_review(work.attempt_id)
        if not _refund_matches(work, refund) or refund.status not in {"pending", "processed"}:
            return await self._manual_review(work.attempt_id)
        return await self._store.store_refund(work.attempt_id, refund)

    async def _manual_review(self, attempt_id: str) -> RefundOutcome:
        await self._store.mark_manual_review(attempt_id)
        return RefundOutcome.MANUAL_REVIEW


def build_refund_receipt(*, attempt_id: str, payment_id: str) -> str:
    digest = hashlib.sha256(f"{attempt_id}|{payment_id}|full".encode()).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"acsarfnd1_{encoded[:26]}"


def _refund_matches(work: RefundWork, refund: ProviderRefundRecord) -> bool:
    return (
        refund.payment_id == work.payment_id
        and refund.amount_minor == work.amount_minor
        and refund.currency == work.currency
        and refund.receipt == work.receipt
        and dict(refund.notes) == dict(work.notes)
    )
