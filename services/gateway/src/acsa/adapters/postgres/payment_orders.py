"""PostgreSQL state boundary for provider Order creation and recovery."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import (
    AuditEvent,
    CheckoutSession,
    PaymentAttempt,
    ProviderOrder,
)
from acsa.domain.commerce import CheckoutStatus, PaymentAttemptState
from acsa.domain.receipts import ProviderOrderCandidate, select_exact_order
from acsa.services.payment_orders import PaymentOrderAction, PaymentOrderWork


class PostgresPaymentOrderStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def prepare(self, attempt_id: str) -> PaymentOrderWork:
        async with self._session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if attempt is None:
                return _empty_work(attempt_id, PaymentOrderAction.NOT_FOUND)
            checkout = await session.get(CheckoutSession, attempt.checkout_id)
            if checkout is None:
                return _empty_work(attempt_id, PaymentOrderAction.NOT_FOUND)
            stored = await session.scalar(
                select(ProviderOrder.id).where(ProviderOrder.attempt_id == attempt.id)
            )
            if stored is not None or attempt.state == PaymentAttemptState.AWAITING_PAYMENT.value:
                return _work(attempt, checkout, PaymentOrderAction.COMPLETE)
            if attempt.state == PaymentAttemptState.DRAFT.value:
                attempt.state = PaymentAttemptState.PROVIDER_ORDER_CREATING.value
                await _append_audit(
                    session,
                    attempt.id,
                    "payment_attempt.provider_order_creating",
                    {"receipt": attempt.receipt},
                )
                return _work(attempt, checkout, PaymentOrderAction.CREATE)
            if attempt.state == PaymentAttemptState.PROVIDER_ORDER_CREATING.value:
                attempt.state = PaymentAttemptState.RECONCILING.value
                attempt.provider_uncertain = True
                await _append_audit(
                    session,
                    attempt.id,
                    "payment_attempt.provider_order_uncertain",
                    {"reason": "interrupted_create"},
                )
                return _work(attempt, checkout, PaymentOrderAction.RECOVER)
            if attempt.state == PaymentAttemptState.RECONCILING.value:
                return _work(attempt, checkout, PaymentOrderAction.RECOVER)
            return _work(attempt, checkout, PaymentOrderAction.COMPLETE)

    async def store_order(
        self,
        attempt_id: str,
        order: ProviderOrderCandidate,
        *,
        recovered: bool,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if attempt is None:
                return False
            checkout = await session.scalar(
                select(CheckoutSession)
                .where(CheckoutSession.id == attempt.checkout_id)
                .with_for_update()
            )
            if checkout is None:
                return False
            existing = await session.scalar(
                select(ProviderOrder).where(ProviderOrder.attempt_id == attempt.id)
            )
            if existing is not None:
                return existing.provider_order_id == order.order_id
            expected_notes = {
                "checkout_id": checkout.id,
                "attempt_id": attempt.id,
                "snapshot_checksum": attempt.snapshot_checksum,
            }
            exact = select_exact_order(
                [order],
                expected_receipt=attempt.receipt,
                expected_amount_minor=attempt.amount_minor,
                expected_currency=attempt.currency,
                expected_notes=expected_notes,
            )
            if exact is None or attempt.state not in {
                PaymentAttemptState.PROVIDER_ORDER_CREATING.value,
                PaymentAttemptState.RECONCILING.value,
            }:
                return False
            session.add(
                ProviderOrder(
                    attempt_id=attempt.id,
                    provider_order_id=exact.order_id,
                    receipt=exact.receipt,
                    amount_minor=exact.amount_minor,
                    currency=exact.currency,
                    notes=dict(exact.notes),
                    recovered=recovered,
                )
            )
            attempt.state = PaymentAttemptState.AWAITING_PAYMENT.value
            attempt.provider_uncertain = False
            checkout.status = CheckoutStatus.PAYMENT_PENDING.value
            checkout.version += 1
            await _append_audit(
                session,
                attempt.id,
                "payment_attempt.provider_order_recovered"
                if recovered
                else "payment_attempt.provider_order_created",
                {"provider_order_id": exact.order_id},
            )
            return True

    async def mark_reconciling(self, attempt_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if attempt is None or attempt.state not in {
                PaymentAttemptState.PROVIDER_ORDER_CREATING.value,
                PaymentAttemptState.RECONCILING.value,
            }:
                return
            attempt.state = PaymentAttemptState.RECONCILING.value
            attempt.provider_uncertain = True
            await _append_audit(
                session,
                attempt.id,
                "payment_attempt.provider_order_uncertain",
                {"reason": "provider_result_unknown"},
            )

    async def mark_failed(self, attempt_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
            )
            if (
                attempt is None
                or attempt.state != PaymentAttemptState.PROVIDER_ORDER_CREATING.value
            ):
                return
            attempt.state = PaymentAttemptState.FAILED.value
            attempt.provider_uncertain = False
            await _append_audit(
                session,
                attempt.id,
                "payment_attempt.provider_order_failed",
                {"reason": "provider_rejected"},
            )


def _work(
    attempt: PaymentAttempt,
    checkout: CheckoutSession,
    action: PaymentOrderAction,
) -> PaymentOrderWork:
    return PaymentOrderWork(
        action=action,
        attempt_id=attempt.id,
        checkout_id=checkout.id,
        merchant_id=checkout.merchant_id,
        receipt=attempt.receipt,
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        snapshot_checksum=attempt.snapshot_checksum,
    )


def _empty_work(attempt_id: str, action: PaymentOrderAction) -> PaymentOrderWork:
    return PaymentOrderWork(
        action=action,
        attempt_id=attempt_id,
        checkout_id="",
        merchant_id="",
        receipt="",
        amount_minor=0,
        currency="",
        snapshot_checksum="",
    )


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
            evidence_source="provider_api",
        )
    )
