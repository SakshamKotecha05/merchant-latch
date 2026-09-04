from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import orjson
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import (
    OutboxJob,
    PaymentAttempt,
    ProviderOrder,
    Refund,
    WebhookEvent,
)
from acsa.ports.webhooks import (
    WebhookFinalizationWork,
    WebhookInsertResult,
    WebhookRefundWork,
)


class PostgresWebhookStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert_verified_event(
        self,
        *,
        event_id: str,
        event_name: str,
        raw_payload: bytes,
        payload_hash: str,
    ) -> WebhookInsertResult:
        payment_id, order_id, refund_id = _extract_provider_references(raw_payload)
        webhook_id = uuid4()
        job_id = uuid4()

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                insert(WebhookEvent)
                .values(
                    id=webhook_id,
                    provider="razorpay",
                    event_id=event_id,
                    event_name=event_name,
                    payload_hash=payload_hash,
                    payment_id=payment_id,
                    order_id=order_id,
                    refund_id=refund_id,
                )
                .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
                .returning(WebhookEvent.id)
            )
            if result.scalar_one_or_none() is None:
                return WebhookInsertResult(created=False, job_id=None)

            session.add(
                OutboxJob(
                    id=job_id,
                    job_type="process_razorpay_webhook",
                    aggregate_type="webhook_event",
                    aggregate_id=str(webhook_id),
                    payload={"webhook_event_id": str(webhook_id)},
                )
            )

        return WebhookInsertResult(created=True, job_id=job_id)

    async def load_finalization_work(
        self, webhook_event_id: UUID
    ) -> WebhookFinalizationWork | None:
        async with self._session_factory() as session:
            event = await session.get(WebhookEvent, webhook_event_id)
            if (
                event is None
                or event.event_name not in {"payment.captured", "order.paid"}
                or event.payment_id is None
                or event.order_id is None
            ):
                return None
            attempt_id = await session.scalar(
                select(ProviderOrder.attempt_id).where(
                    ProviderOrder.provider_order_id == event.order_id
                )
            )
            if attempt_id is None:
                return None
            return WebhookFinalizationWork(
                attempt_id=attempt_id,
                payment_id=event.payment_id,
                order_id=event.order_id,
            )

    async def load_refund_work(self, webhook_event_id: UUID) -> WebhookRefundWork | None:
        async with self._session_factory() as session:
            event = await session.get(WebhookEvent, webhook_event_id)
            if (
                event is None
                or not event.event_name.startswith("refund.")
                or event.refund_id is None
                or event.payment_id is None
            ):
                return None
            attempt_id = await session.scalar(
                select(Refund.attempt_id).where(Refund.provider_refund_id == event.refund_id)
            )
            if attempt_id is None:
                attempt_id = await session.scalar(
                    select(PaymentAttempt.id).where(
                        PaymentAttempt.provider_payment_id == event.payment_id
                    )
                )
            if attempt_id is None:
                return None
            return WebhookRefundWork(attempt_id=attempt_id, refund_id=event.refund_id)

    async def mark_processed(self, webhook_event_id: UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(WebhookEvent)
                .where(
                    WebhookEvent.id == webhook_event_id,
                    WebhookEvent.processed_at.is_(None),
                )
                .values(processed_at=func.now())
                .returning(WebhookEvent.id)
            )
            if result.scalar_one_or_none() is not None:
                return True
            existing_id = await session.scalar(
                select(WebhookEvent.id).where(WebhookEvent.id == webhook_event_id)
            )
            return existing_id is not None


def _extract_provider_references(
    raw_payload: bytes,
) -> tuple[str | None, str | None, str | None]:
    payload: dict[str, Any] = orjson.loads(raw_payload)
    nested_payload = payload.get("payload", {})
    payment = _entity(nested_payload, "payment")
    order = _entity(nested_payload, "order")
    refund = _entity(nested_payload, "refund")
    payment_id = _string_or_none(payment.get("id")) or _string_or_none(refund.get("payment_id"))
    order_id = _string_or_none(payment.get("order_id")) or _string_or_none(order.get("id"))
    refund_id = _string_or_none(refund.get("id"))
    return payment_id, order_id, refund_id


def _entity(payload: object, key: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    wrapper = payload.get(key)
    if not isinstance(wrapper, dict):
        return {}
    entity = wrapper.get("entity")
    return entity if isinstance(entity, dict) else {}


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
