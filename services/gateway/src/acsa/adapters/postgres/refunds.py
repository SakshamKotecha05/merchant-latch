"""PostgreSQL state boundary for lease expiry and full refunds."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import (
    AuditEvent,
    CheckoutLine,
    CheckoutSession,
    Inventory,
    InventoryLease,
    PaymentAttempt,
    Refund,
)
from acsa.domain.commerce import CheckoutStatus, InventoryLeaseState, PaymentAttemptState
from acsa.domain.payments import ProviderRefundRecord
from acsa.services.refunds import (
    RefundAction,
    RefundOutcome,
    RefundWork,
    build_refund_receipt,
)


class PostgresRefundStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def prepare(self, attempt_id: str) -> RefundWork:
        async with self._session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if attempt is None or attempt.provider_payment_id is None:
                return _empty_work(attempt_id, RefundAction.NOT_FOUND)
            refund = await session.scalar(select(Refund).where(Refund.attempt_id == attempt.id))
            receipt = build_refund_receipt(
                attempt_id=attempt.id,
                payment_id=attempt.provider_payment_id,
            )
            if attempt.state in {
                PaymentAttemptState.REFUNDED.value,
                PaymentAttemptState.MANUAL_REVIEW.value,
            }:
                return _work(attempt, receipt, RefundAction.COMPLETE, refund)
            if refund is not None:
                return _work(attempt, receipt, RefundAction.FETCH, refund)
            if attempt.state == PaymentAttemptState.PAID_INVENTORY_EXCEPTION.value:
                attempt.state = PaymentAttemptState.REFUND_PENDING.value
                await _append_audit(
                    session,
                    attempt.id,
                    "payment_attempt.refund_pending",
                    {"receipt": receipt},
                )
                return _work(attempt, receipt, RefundAction.CREATE, None)
            if attempt.state == PaymentAttemptState.REFUND_PENDING.value:
                return _work(attempt, receipt, RefundAction.CREATE, None)
            return _work(attempt, receipt, RefundAction.COMPLETE, refund)

    async def store_refund(
        self,
        attempt_id: str,
        refund: ProviderRefundRecord,
    ) -> RefundOutcome:
        async with self._session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if attempt is None or attempt.provider_payment_id is None:
                return RefundOutcome.NOT_FOUND
            expected_receipt = build_refund_receipt(
                attempt_id=attempt.id,
                payment_id=attempt.provider_payment_id,
            )
            expected_notes = {"attempt_id": attempt.id, "reason": "inventory_exception"}
            if (
                refund.payment_id != attempt.provider_payment_id
                or refund.amount_minor != attempt.amount_minor
                or refund.currency != attempt.currency
                or refund.receipt != expected_receipt
                or dict(refund.notes) != expected_notes
                or refund.status not in {"pending", "processed"}
            ):
                attempt.state = PaymentAttemptState.MANUAL_REVIEW.value
                return RefundOutcome.MANUAL_REVIEW
            existing = await session.scalar(select(Refund).where(Refund.attempt_id == attempt.id))
            if existing is None:
                existing = Refund(
                    attempt_id=attempt.id,
                    provider_refund_id=refund.refund_id,
                    provider_payment_id=refund.payment_id,
                    receipt=refund.receipt,
                    amount_minor=refund.amount_minor,
                    currency=refund.currency,
                    status=refund.status,
                )
                session.add(existing)
            elif existing.provider_refund_id != refund.refund_id:
                attempt.state = PaymentAttemptState.MANUAL_REVIEW.value
                return RefundOutcome.MANUAL_REVIEW
            else:
                existing.status = refund.status
            if refund.status == "processed":
                attempt.state = PaymentAttemptState.REFUNDED.value
                await _append_audit(
                    session,
                    attempt.id,
                    "payment_attempt.refunded",
                    {"refund_id": refund.refund_id},
                )
                return RefundOutcome.REFUNDED
            attempt.state = PaymentAttemptState.REFUND_PENDING.value
            return RefundOutcome.PENDING

    async def mark_manual_review(self, attempt_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if attempt is None or attempt.state not in {
                PaymentAttemptState.PAID_INVENTORY_EXCEPTION.value,
                PaymentAttemptState.REFUND_PENDING.value,
            }:
                return
            attempt.state = PaymentAttemptState.MANUAL_REVIEW.value
            await _append_audit(
                session,
                attempt.id,
                "payment_attempt.refund_manual_review",
                {"reason": "provider_refund_conflict"},
            )

    async def release_expired_leases(self, *, limit: int) -> int:
        async with self._session_factory() as session:
            attempt_ids = list(
                await session.scalars(
                    select(InventoryLease.attempt_id)
                    .where(
                        InventoryLease.state == InventoryLeaseState.ACTIVE.value,
                        InventoryLease.expires_at <= func.now(),
                    )
                    .order_by(InventoryLease.expires_at, InventoryLease.attempt_id)
                    .limit(limit)
                )
            )
        released = 0
        for attempt_id in attempt_ids:
            released += int(await self._release_one(attempt_id))
        return released

    async def _release_one(self, attempt_id: str) -> bool:
        async with self._session_factory() as session, session.begin():
            checkout_id = await session.scalar(
                select(PaymentAttempt.checkout_id).where(PaymentAttempt.id == attempt_id)
            )
            if checkout_id is None:
                return False
            checkout = await session.scalar(
                select(CheckoutSession).where(CheckoutSession.id == checkout_id).with_for_update()
            )
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if checkout is None or attempt is None:
                return False
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
            now = await session.scalar(select(func.now()))
            if (
                lease is None
                or not isinstance(now, datetime)
                or lease.state != InventoryLeaseState.ACTIVE.value
                or lease.expires_at > now
                or not lines
                or len(inventory) != len(variant_ids)
                or any(inventory[line.variant_id].reserved < line.quantity for line in lines)
            ):
                return False
            for line in lines:
                row = inventory[line.variant_id]
                row.reserved -= line.quantity
                row.version += 1
            lease.state = InventoryLeaseState.EXPIRED.value
            lease.released_at = now
            if attempt.state in {
                PaymentAttemptState.AWAITING_PAYMENT.value,
                PaymentAttemptState.VERIFYING.value,
                PaymentAttemptState.RECONCILING.value,
            }:
                attempt.state = PaymentAttemptState.EXPIRED.value
            if checkout.status == CheckoutStatus.PAYMENT_PENDING.value:
                checkout.status = CheckoutStatus.EXPIRED.value
                checkout.version += 1
            await _append_audit(
                session,
                attempt.id,
                "payment_attempt.lease_expired",
                {"lease_id": str(lease.id)},
            )
            return True


def _work(
    attempt: PaymentAttempt,
    receipt: str,
    action: RefundAction,
    refund: Refund | None,
) -> RefundWork:
    return RefundWork(
        action=action,
        attempt_id=attempt.id,
        payment_id=attempt.provider_payment_id or "",
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        receipt=receipt,
        provider_refund_id=refund.provider_refund_id if refund is not None else None,
    )


def _empty_work(attempt_id: str, action: RefundAction) -> RefundWork:
    return RefundWork(action, attempt_id, "", 0, "", "")


async def _append_audit(
    session: AsyncSession,
    attempt_id: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    sequence = await session.scalar(
        select(func.coalesce(func.max(AuditEvent.sequence), 0)).where(
            AuditEvent.aggregate_type == "payment_attempt",
            AuditEvent.aggregate_id == attempt_id,
        )
    )
    session.add(
        AuditEvent(
            aggregate_type="payment_attempt",
            aggregate_id=attempt_id,
            sequence=int(sequence or 0) + 1,
            event_type=event_type,
            payload=payload,
            evidence_source="merchant_policy",
        )
    )
