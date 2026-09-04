"""PostgreSQL convergence boundary for captured provider payments."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import (
    AuditEvent,
    CheckoutLine,
    CheckoutSession,
    Inventory,
    InventoryLease,
    MerchantOrder,
    OutboxJob,
    PaymentAttempt,
    ProviderOrder,
)
from acsa.domain.commerce import CheckoutStatus, InventoryLeaseState, PaymentAttemptState
from acsa.domain.payments import ProviderPaymentRecord
from acsa.services.payment_finalization import (
    EvidenceSource,
    FinalizationAction,
    FinalizationOutcome,
    FinalizationWork,
)


class PostgresPaymentFinalizationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_work(self, attempt_id: str) -> FinalizationWork:
        async with self._session_factory() as session:
            attempt = await session.get(PaymentAttempt, attempt_id)
            if attempt is None:
                return _empty_work(attempt_id, FinalizationAction.NOT_FOUND)
            provider_order = await session.scalar(
                select(ProviderOrder).where(ProviderOrder.attempt_id == attempt.id)
            )
            if provider_order is None:
                return _empty_work(attempt_id, FinalizationAction.NOT_FOUND)
            action = (
                FinalizationAction.REPLAY
                if attempt.state
                in {
                    PaymentAttemptState.PAID.value,
                    PaymentAttemptState.PAID_INVENTORY_EXCEPTION.value,
                    PaymentAttemptState.REFUND_PENDING.value,
                    PaymentAttemptState.REFUNDED.value,
                }
                else FinalizationAction.FINALIZE
            )
            return _work(attempt, provider_order, action)

    async def mark_verifying(self, attempt_id: str) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(PaymentAttempt)
                .where(
                    PaymentAttempt.id == attempt_id,
                    PaymentAttempt.state.in_(
                        [
                            PaymentAttemptState.AWAITING_PAYMENT.value,
                            PaymentAttemptState.VERIFYING.value,
                            PaymentAttemptState.RECONCILING.value,
                            PaymentAttemptState.EXPIRED.value,
                            PaymentAttemptState.CANCELED.value,
                        ]
                    ),
                )
                .values(state=PaymentAttemptState.VERIFYING.value)
                .returning(PaymentAttempt.id)
            )
            return result.scalar_one_or_none() is not None

    async def finalize(
        self,
        attempt_id: str,
        *,
        provider_account_id: str,
        payment: ProviderPaymentRecord,
        evidence_source: EvidenceSource,
        evidence_digest: str,
    ) -> FinalizationOutcome:
        async with self._session_factory() as session, session.begin():
            checkout_id = await session.scalar(
                select(PaymentAttempt.checkout_id).where(PaymentAttempt.id == attempt_id)
            )
            if checkout_id is None:
                return FinalizationOutcome.NOT_FOUND
            checkout = await session.scalar(
                select(CheckoutSession).where(CheckoutSession.id == checkout_id).with_for_update()
            )
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if checkout is None or attempt is None:
                return FinalizationOutcome.NOT_FOUND
            if checkout.status == CheckoutStatus.COMPLETED.value or attempt.state in {
                PaymentAttemptState.PAID.value,
                PaymentAttemptState.PAID_INVENTORY_EXCEPTION.value,
                PaymentAttemptState.REFUND_PENDING.value,
                PaymentAttemptState.REFUNDED.value,
            }:
                return FinalizationOutcome.REPLAYED
            conflicting_attempt = await session.scalar(
                select(PaymentAttempt.id).where(
                    PaymentAttempt.provider_account_id == provider_account_id,
                    PaymentAttempt.provider_payment_id == payment.payment_id,
                    PaymentAttempt.id != attempt.id,
                )
            )
            if conflicting_attempt is not None:
                return FinalizationOutcome.RECONCILING
            lines = list(
                await session.scalars(
                    select(CheckoutLine)
                    .where(CheckoutLine.checkout_id == checkout.id)
                    .order_by(CheckoutLine.variant_id)
                )
            )
            variant_ids = [line.variant_id for line in lines]
            inventory_rows = list(
                await session.scalars(
                    select(Inventory)
                    .where(Inventory.variant_id.in_(variant_ids))
                    .order_by(Inventory.variant_id)
                    .with_for_update()
                )
            )
            inventory = {row.variant_id: row for row in inventory_rows}
            lease = await session.scalar(
                select(InventoryLease)
                .where(InventoryLease.attempt_id == attempt.id)
                .with_for_update()
            )
            if lease is None:
                return FinalizationOutcome.RECONCILING
            provider_order = await session.scalar(
                select(ProviderOrder).where(ProviderOrder.attempt_id == attempt.id)
            )
            if provider_order is None:
                return FinalizationOutcome.RECONCILING
            now = await session.scalar(select(func.now()))
            if not isinstance(now, datetime):
                return FinalizationOutcome.RECONCILING
            if lease.state == InventoryLeaseState.ACTIVE.value and (
                not lines
                or len(inventory) != len(variant_ids)
                or any(inventory[line.variant_id].reserved < line.quantity for line in lines)
            ):
                return FinalizationOutcome.RECONCILING
            lease_expired = (
                lease.state == InventoryLeaseState.ACTIVE.value and lease.expires_at <= now
            )
            if lease_expired:
                for line in lines:
                    row = inventory[line.variant_id]
                    row.reserved -= line.quantity
                    row.version += 1
                lease.state = InventoryLeaseState.EXPIRED.value
                lease.released_at = now
            attempt.provider_account_id = provider_account_id
            attempt.provider_payment_id = payment.payment_id
            attempt.payment_evidence_digest = evidence_digest
            attempt.payment_evidence_source = evidence_source.kind.value
            if lease.state != InventoryLeaseState.ACTIVE.value:
                attempt.state = PaymentAttemptState.PAID_INVENTORY_EXCEPTION.value
                session.add(
                    OutboxJob(
                        job_type="refund_captured_payment",
                        aggregate_type="payment_attempt",
                        aggregate_id=attempt.id,
                        payload={"attempt_id": attempt.id},
                    )
                )
                await _append_audit(
                    session,
                    attempt.id,
                    "payment_attempt.paid_inventory_exception",
                    {"evidence_digest": evidence_digest},
                    evidence_source.kind.value,
                )
                return FinalizationOutcome.INVENTORY_EXCEPTION
            for line in lines:
                row = inventory[line.variant_id]
                row.reserved -= line.quantity
                row.sold += line.quantity
                row.version += 1
            lease.state = InventoryLeaseState.CONSUMED.value
            lease.consumed_at = now
            session.add(
                MerchantOrder(
                    checkout_id=checkout.id,
                    attempt_id=attempt.id,
                    provider_account_id=provider_account_id,
                    provider_order_id=provider_order.provider_order_id,
                    provider_payment_id=payment.payment_id,
                    amount_minor=payment.amount_minor,
                    currency=payment.currency,
                    evidence_digest=evidence_digest,
                    evidence_source=evidence_source.kind.value,
                )
            )
            attempt.state = PaymentAttemptState.PAID.value
            checkout.status = CheckoutStatus.COMPLETED.value
            checkout.version += 1
            for job_type in ("send_order_confirmation", "record_payment_evidence"):
                session.add(
                    OutboxJob(
                        job_type=job_type,
                        aggregate_type="payment_attempt",
                        aggregate_id=attempt.id,
                        payload={"attempt_id": attempt.id},
                    )
                )
            await _append_audit(
                session,
                attempt.id,
                "payment_attempt.paid",
                {"evidence_digest": evidence_digest},
                evidence_source.kind.value,
            )
            await _append_audit(
                session,
                checkout.id,
                "checkout.completed",
                {"attempt_id": attempt.id},
                evidence_source.kind.value,
            )
            return FinalizationOutcome.COMPLETED

    async def mark_reconciling(self, attempt_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(PaymentAttempt)
                .where(
                    PaymentAttempt.id == attempt_id,
                    PaymentAttempt.state == PaymentAttemptState.VERIFYING.value,
                )
                .values(
                    state=PaymentAttemptState.RECONCILING.value,
                    provider_uncertain=True,
                )
            )


def _work(
    attempt: PaymentAttempt,
    provider_order: ProviderOrder,
    action: FinalizationAction,
) -> FinalizationWork:
    return FinalizationWork(
        action=action,
        attempt_id=attempt.id,
        checkout_id=attempt.checkout_id,
        provider_order_id=provider_order.provider_order_id,
        receipt=provider_order.receipt,
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        snapshot_checksum=attempt.snapshot_checksum,
    )


def _empty_work(attempt_id: str, action: FinalizationAction) -> FinalizationWork:
    return FinalizationWork(action, attempt_id, "", "", "", 0, "", "")


async def _append_audit(
    session: AsyncSession,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, object],
    evidence_source: str,
) -> None:
    aggregate_type = "payment_attempt" if aggregate_id.startswith("att_") else "checkout"
    sequence = await session.scalar(
        select(func.coalesce(func.max(AuditEvent.sequence), 0)).where(
            AuditEvent.aggregate_type == aggregate_type,
            AuditEvent.aggregate_id == aggregate_id,
        )
    )
    session.add(
        AuditEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            sequence=int(sequence or 0) + 1,
            event_type=event_type,
            payload=payload,
            evidence_source=evidence_source,
        )
    )
