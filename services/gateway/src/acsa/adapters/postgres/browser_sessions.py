"""Durable continuation redemption and bounded merchant-facing read models."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import (
    AuditEvent,
    CheckoutSession,
    InventoryLease,
    MerchantBrowserSession,
    MerchantOrder,
    PaymentAttempt,
    PickupLocation,
)
from acsa.security.browser_sessions import BrowserIdentity


class PostgresBrowserSessionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def redeem(
        self,
        *,
        token_digest: str,
        continuation_digest: str,
        csrf_digest: str,
        checkout_id: str,
        checkout_version: int,
        now: datetime,
        approval_expires_at: datetime,
    ) -> bool:
        async with self._sessions() as session, session.begin():
            checkout = await session.scalar(
                select(CheckoutSession).where(CheckoutSession.id == checkout_id).with_for_update()
            )
            if (
                checkout is None
                or checkout.version != checkout_version
                or checkout.expires_at <= now
                or approval_expires_at <= now
            ):
                return False
            result = await session.execute(
                insert(MerchantBrowserSession)
                .values(
                    token_digest=token_digest,
                    continuation_digest=continuation_digest,
                    csrf_digest=csrf_digest,
                    checkout_id=checkout_id,
                    checkout_version=checkout_version,
                    review_at=now,
                    approval_expires_at=min(approval_expires_at, checkout.expires_at),
                    expires_at=now + timedelta(hours=1),
                )
                .on_conflict_do_nothing()
                .returning(MerchantBrowserSession.token_digest)
            )
            return result.scalar_one_or_none() is not None

    async def authenticate(self, digest: str) -> BrowserIdentity | None:
        async with self._sessions() as session:
            record = await session.scalar(
                select(MerchantBrowserSession).where(
                    MerchantBrowserSession.token_digest == digest,
                    MerchantBrowserSession.expires_at > func.now(),
                )
            )
            if record is None:
                return None
            return BrowserIdentity(
                record.checkout_id,
                record.checkout_version,
                record.review_at,
                record.approval_expires_at,
                record.expires_at,
                record.csrf_digest,
            )

    async def owns_attempt(self, checkout_id: str, attempt_id: str) -> bool:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(PaymentAttempt.id).where(
                        PaymentAttempt.id == attempt_id,
                        PaymentAttempt.checkout_id == checkout_id,
                    )
                )
                is not None
            )

    async def status(self, checkout_id: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            checkout = await session.get(CheckoutSession, checkout_id)
            if checkout is None:
                return None
            pickup = await session.get(PickupLocation, checkout.pickup_location_id)
            attempt = await session.scalar(
                select(PaymentAttempt)
                .where(PaymentAttempt.checkout_id == checkout_id)
                .order_by(PaymentAttempt.attempt_version.desc())
                .limit(1)
            )
            lease = (
                None
                if attempt is None
                else await session.scalar(
                    select(InventoryLease).where(InventoryLease.attempt_id == attempt.id)
                )
            )
            order = await session.scalar(
                select(MerchantOrder).where(MerchantOrder.checkout_id == checkout_id)
            )
            events = list(
                await session.scalars(
                    select(AuditEvent)
                    .where(
                        or_(
                            (AuditEvent.aggregate_type == "checkout")
                            & (AuditEvent.aggregate_id == checkout_id),
                            (AuditEvent.aggregate_type == "payment_attempt")
                            & (AuditEvent.aggregate_id == (attempt.id if attempt else "")),
                        )
                    )
                    .order_by(AuditEvent.created_at, AuditEvent.sequence)
                    .limit(100)
                )
            )
            return {
                "checkout_id": checkout.id,
                "status": checkout.status,
                "version": checkout.version,
                "expires_at": checkout.expires_at.isoformat(),
                "pickup": None if pickup is None else {"name": pickup.name, "city": pickup.city},
                "attempt": None if attempt is None else {"id": attempt.id, "state": attempt.state},
                "lease_expires_at": None if lease is None else lease.expires_at.isoformat(),
                "order": None
                if order is None
                else {
                    "id": str(order.id),
                    "amount": order.amount_minor,
                    "currency": order.currency,
                },
                "events": [
                    {
                        "type": event.event_type,
                        "source": event.evidence_source,
                        "at": event.created_at.isoformat(),
                    }
                    for event in events
                ],
            }

    async def public_order(self, order_id: UUID) -> dict[str, Any] | None:
        async with self._sessions() as session:
            order = await session.get(MerchantOrder, order_id)
            if order is None:
                return None
            return {
                "id": str(order.id),
                "status": "completed",
                "amount": order.amount_minor,
                "currency": order.currency,
            }
