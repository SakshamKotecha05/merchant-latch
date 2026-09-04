"""Verify provider evidence and converge every payment signal on one transaction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from acsa.adapters.razorpay.client import RazorpayProviderError
from acsa.domain.canonical import sha256_checksum
from acsa.domain.payments import ProviderOrderRecord, ProviderPaymentRecord
from acsa.security.razorpay_signatures import verify_checkout_signature


class EvidenceKind(StrEnum):
    BROWSER = "browser"
    WEBHOOK = "webhook"
    RECONCILIATION = "reconciliation"


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    kind: EvidenceKind
    payment_id: str
    order_id: str | None = None
    signature: str | None = None
    webhook_event_id: str | None = None

    @classmethod
    def browser(cls, *, payment_id: str, signature: str) -> EvidenceSource:
        return cls(EvidenceKind.BROWSER, payment_id=payment_id, signature=signature)

    @classmethod
    def webhook(
        cls,
        *,
        payment_id: str,
        order_id: str,
        webhook_event_id: str | None = None,
    ) -> EvidenceSource:
        return cls(
            EvidenceKind.WEBHOOK,
            payment_id=payment_id,
            order_id=order_id,
            webhook_event_id=webhook_event_id,
        )


class FinalizationAction(StrEnum):
    FINALIZE = "finalize"
    REPLAY = "replay"
    NOT_FOUND = "not_found"


class FinalizationOutcome(StrEnum):
    COMPLETED = "completed"
    REPLAYED = "replayed"
    INVENTORY_EXCEPTION = "inventory_exception"
    RECONCILING = "reconciling"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class FinalizationWork:
    action: FinalizationAction
    attempt_id: str
    checkout_id: str
    provider_order_id: str
    receipt: str
    amount_minor: int
    currency: str
    snapshot_checksum: str
    launch_allowed: bool = False

    @property
    def expected_notes(self) -> dict[str, str]:
        return {
            "checkout_id": self.checkout_id,
            "attempt_id": self.attempt_id,
            "snapshot_checksum": self.snapshot_checksum,
        }


@dataclass(frozen=True, slots=True)
class PaymentLaunchConfiguration:
    checkout_id: str
    attempt_id: str
    provider_key_id: str
    provider_order_id: str
    amount_minor: int
    currency: str


class PaymentFinalizationStorePort(Protocol):
    async def load_work(self, attempt_id: str) -> FinalizationWork: ...

    async def mark_verifying(self, attempt_id: str) -> bool: ...

    async def finalize(
        self,
        attempt_id: str,
        *,
        provider_account_id: str,
        payment: ProviderPaymentRecord,
        evidence_source: EvidenceSource,
        evidence_digest: str,
    ) -> FinalizationOutcome: ...

    async def mark_reconciling(self, attempt_id: str) -> None: ...


class PaymentEvidenceProviderPort(Protocol):
    async def fetch_payment(self, payment_id: str) -> ProviderPaymentRecord: ...

    async def fetch_order(self, order_id: str) -> ProviderOrderRecord: ...


class PaymentFinalizationService:
    def __init__(
        self,
        *,
        store: PaymentFinalizationStorePort,
        provider: PaymentEvidenceProviderPort,
        provider_account_id: str,
        checkout_secret: str,
    ) -> None:
        self._store = store
        self._provider = provider
        self._provider_account_id = provider_account_id
        self._checkout_secret = checkout_secret

    async def payment_launch_configuration(
        self, payment_attempt_id: str
    ) -> PaymentLaunchConfiguration | None:
        work = await self._store.load_work(payment_attempt_id)
        if work.action is not FinalizationAction.FINALIZE or not work.launch_allowed:
            return None
        return PaymentLaunchConfiguration(
            checkout_id=work.checkout_id,
            attempt_id=work.attempt_id,
            provider_key_id=self._provider_account_id,
            provider_order_id=work.provider_order_id,
            amount_minor=work.amount_minor,
            currency=work.currency,
        )

    async def finalize_payment(
        self,
        payment_attempt_id: str,
        evidence_source: EvidenceSource,
    ) -> FinalizationOutcome:
        work = await self._store.load_work(payment_attempt_id)
        if work.action is FinalizationAction.NOT_FOUND:
            return FinalizationOutcome.NOT_FOUND
        if work.action is FinalizationAction.REPLAY:
            return FinalizationOutcome.REPLAYED
        if not self._source_matches(work, evidence_source):
            return FinalizationOutcome.REJECTED
        if not await self._store.mark_verifying(payment_attempt_id):
            return FinalizationOutcome.REPLAYED
        try:
            payment = await self._provider.fetch_payment(evidence_source.payment_id)
            order = await self._provider.fetch_order(work.provider_order_id)
        except (httpx.TimeoutException, RazorpayProviderError):
            await self._store.mark_reconciling(payment_attempt_id)
            return FinalizationOutcome.RECONCILING
        if not _evidence_matches(work, evidence_source, payment, order):
            await self._store.mark_reconciling(payment_attempt_id)
            return FinalizationOutcome.RECONCILING
        digest = sha256_checksum(
            {
                "provider_account_id": self._provider_account_id,
                "provider_order_id": order.order_id,
                "provider_payment_id": payment.payment_id,
                "amount_minor": payment.amount_minor,
                "currency": payment.currency,
                "payment_status": payment.status,
                "order_status": order.status,
            }
        )
        outcome = await self._store.finalize(
            payment_attempt_id,
            provider_account_id=self._provider_account_id,
            payment=payment,
            evidence_source=evidence_source,
            evidence_digest=digest,
        )
        if outcome is FinalizationOutcome.RECONCILING:
            await self._store.mark_reconciling(payment_attempt_id)
        return outcome

    def _source_matches(self, work: FinalizationWork, source: EvidenceSource) -> bool:
        if not source.payment_id:
            return False
        if source.order_id is not None and source.order_id != work.provider_order_id:
            return False
        if source.kind is EvidenceKind.BROWSER:
            return source.signature is not None and verify_checkout_signature(
                stored_order_id=work.provider_order_id,
                payment_id=source.payment_id,
                signature=source.signature,
                key_secret=self._checkout_secret,
            )
        return source.kind in {EvidenceKind.WEBHOOK, EvidenceKind.RECONCILIATION}


def _evidence_matches(
    work: FinalizationWork,
    source: EvidenceSource,
    payment: ProviderPaymentRecord,
    order: ProviderOrderRecord,
) -> bool:
    return (
        payment.payment_id == source.payment_id
        and payment.order_id == work.provider_order_id
        and payment.amount_minor == work.amount_minor
        and payment.currency == work.currency
        and payment.status == "captured"
        and payment.captured is True
        and order.order_id == work.provider_order_id
        and order.status == "paid"
        and order.receipt == work.receipt
        and order.amount_minor == work.amount_minor
        and order.currency == work.currency
        and dict(order.notes) == work.expected_notes
    )
