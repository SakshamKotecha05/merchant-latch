from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import UCPCheckout, UCPIdempotencyRecord, UCPRequestNonce
from acsa.domain.ucp_checkout import StoredCheckout
from acsa.ports.ucp_checkouts import (
    CheckoutPersistenceOutcome,
    CheckoutPersistenceResult,
)


class PostgresUCPCheckoutStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_or_replay(
        self,
        *,
        buyer_key_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        idempotency_key: str,
        request_sha256: str,
        checkout: StoredCheckout,
    ) -> CheckoutPersistenceResult:
        async with self._session_factory() as session, session.begin():
            await _lock(session, f"idempotency:{buyer_key_id}:{idempotency_key}")
            existing = await session.scalar(
                select(UCPIdempotencyRecord).where(
                    UCPIdempotencyRecord.buyer_key_id == buyer_key_id,
                    UCPIdempotencyRecord.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    return CheckoutPersistenceResult(CheckoutPersistenceOutcome.CONFLICT, None)
                return CheckoutPersistenceResult(
                    CheckoutPersistenceOutcome.REPLAYED,
                    await _get_in_session(session, existing.checkout_id),
                )

            await _lock(session, f"nonce:{buyer_key_id}:{nonce}")
            nonce_exists = await session.scalar(
                select(UCPRequestNonce.checkout_id).where(
                    UCPRequestNonce.buyer_key_id == buyer_key_id,
                    UCPRequestNonce.nonce == nonce,
                )
            )
            if nonce_exists is not None:
                return CheckoutPersistenceResult(CheckoutPersistenceOutcome.NONCE_REPLAY, None)

            record = UCPCheckout(
                id=checkout.id,
                buyer_key_id=buyer_key_id,
                status=checkout.status,
                continue_url=checkout.continue_url,
                expires_at=checkout.expires_at,
                resource=checkout.resource,
                response_body=checkout.response_body,
            )
            session.add(record)
            session.add(
                UCPRequestNonce(
                    buyer_key_id=buyer_key_id,
                    nonce=nonce,
                    expires_at=nonce_expires_at,
                    checkout_id=record.id,
                )
            )
            session.add(
                UCPIdempotencyRecord(
                    buyer_key_id=buyer_key_id,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    checkout_id=record.id,
                )
            )
            return CheckoutPersistenceResult(CheckoutPersistenceOutcome.CREATED, checkout)

    async def get(self, checkout_id: str) -> StoredCheckout | None:
        async with self._session_factory() as session:
            return await _get_in_session(session, checkout_id)


async def _lock(session: AsyncSession, value: str) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(value, 0))))


async def _get_in_session(session: AsyncSession, checkout_id: str) -> StoredCheckout | None:
    record = await session.scalar(select(UCPCheckout).where(UCPCheckout.id == checkout_id))
    if record is None:
        return None
    return StoredCheckout(
        id=record.id,
        buyer_key_id=record.buyer_key_id,
        status=record.status,
        continue_url=record.continue_url,
        expires_at=record.expires_at,
        resource=dict(record.resource),
        response_body=bytes(record.response_body),
    )
