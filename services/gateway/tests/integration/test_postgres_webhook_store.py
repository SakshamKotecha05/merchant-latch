from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import func, select

from acsa.adapters.postgres.models import OutboxJob, WebhookEvent
from acsa.adapters.postgres.webhooks import PostgresWebhookStore

pytestmark = pytest.mark.integration


async def test_duplicate_event_creates_one_webhook_and_one_outbox_job(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = PostgresWebhookStore(session_factory)
    raw_payload = (
        b'{"event":"payment.captured","payload":{"payment":{"entity":'
        b'{"id":"pay_fixture","order_id":"order_fixture"}}}}'
    )
    payload_hash = hashlib.sha256(raw_payload).hexdigest()

    first = await store.insert_verified_event(
        event_id="evt_duplicate_fixture",
        event_name="payment.captured",
        raw_payload=raw_payload,
        payload_hash=payload_hash,
    )
    second = await store.insert_verified_event(
        event_id="evt_duplicate_fixture",
        event_name="payment.captured",
        raw_payload=raw_payload,
        payload_hash=payload_hash,
    )

    assert first.created is True
    assert first.job_id is not None
    assert second.created is False
    assert second.job_id is None

    async with session_factory() as session:
        webhook_count = await session.scalar(select(func.count()).select_from(WebhookEvent))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxJob))
        event = await session.scalar(select(WebhookEvent))

    assert webhook_count == 1
    assert outbox_count == 1
    assert event is not None
    assert event.payment_id == "pay_fixture"
    assert event.order_id == "order_fixture"
    assert event.payload_hash == payload_hash


async def test_mark_processed_uses_the_database_clock_and_is_idempotent(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = PostgresWebhookStore(session_factory)
    raw_payload = b'{"event":"payment.captured","payload":{}}'
    created = await store.insert_verified_event(
        event_id="evt_processing_fixture",
        event_name="payment.captured",
        raw_payload=raw_payload,
        payload_hash=hashlib.sha256(raw_payload).hexdigest(),
    )

    assert created.created is True
    async with session_factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.event_id == "evt_processing_fixture")
        )
    assert event is not None
    assert event.processed_at is None

    first_result = await store.mark_processed(event.id)
    async with session_factory() as session:
        first_processed_at = await session.scalar(
            select(WebhookEvent.processed_at).where(WebhookEvent.id == event.id)
        )
    second_result = await store.mark_processed(event.id)
    async with session_factory() as session:
        second_processed_at = await session.scalar(
            select(WebhookEvent.processed_at).where(WebhookEvent.id == event.id)
        )

    assert first_result is True
    assert second_result is True
    assert first_processed_at is not None
    assert second_processed_at == first_processed_at


async def test_refund_webhook_stores_provider_refund_and_payment_references(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = PostgresWebhookStore(session_factory)
    raw_payload = (
        b'{"event":"refund.processed","payload":{"refund":{"entity":'
        b'{"id":"rfnd_fixture","payment_id":"pay_fixture"}}}}'
    )

    created = await store.insert_verified_event(
        event_id="evt_refund_fixture",
        event_name="refund.processed",
        raw_payload=raw_payload,
        payload_hash=hashlib.sha256(raw_payload).hexdigest(),
    )

    assert created.created is True
    async with session_factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(WebhookEvent.event_id == "evt_refund_fixture")
        )
    assert event is not None
    assert event.payment_id == "pay_fixture"
    assert event.refund_id == "rfnd_fixture"
