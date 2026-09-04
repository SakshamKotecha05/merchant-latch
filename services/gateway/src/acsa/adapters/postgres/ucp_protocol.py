"""PostgreSQL trust pins and redacted UCP exchange evidence."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import UCPExchangeEvent, UCPTrustPin
from acsa.ucp_profiles import BuyerIdentity


class UCPTrustError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The buyer trust identity does not match.")


@dataclass(frozen=True, slots=True)
class TrustPin:
    origin: str
    profile_url: str
    key_id: str
    fingerprint: str
    version: str
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class NewUCPExchange:
    method: str
    route: str
    profile_origin: str | None
    profile_url_sha256: str | None
    buyer_key_id: str | None
    buyer_fingerprint: str | None
    nonce_sha256: str | None
    request_sha256: str
    response_sha256: str | None
    http_status: int
    outcome: str
    checkout_id: str | None
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class UCPExchange:
    id: UUID
    method: str
    route: str
    profile_origin: str | None
    profile_url_sha256: str | None
    buyer_key_id: str | None
    buyer_fingerprint: str | None
    nonce_sha256: str | None
    request_sha256: str
    response_sha256: str | None
    http_status: int
    outcome: str
    checkout_id: str | None
    started_at: datetime
    completed_at: datetime


class PostgresUCPProtocolStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_pin(self, origin: str) -> TrustPin | None:
        async with self._session_factory() as session:
            record = await session.get(UCPTrustPin, origin)
            return _pin(record) if record is not None and record.active else None

    async def verify_or_pin(self, identity: BuyerIdentity, now: datetime) -> TrustPin:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                insert(UCPTrustPin)
                .values(
                    origin=identity.origin,
                    profile_url=identity.profile_url,
                    key_id=identity.key_id,
                    fingerprint=identity.fingerprint,
                    ucp_version=identity.version,
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                .on_conflict_do_nothing(index_elements=[UCPTrustPin.origin])
            )
            record = await session.scalar(
                select(UCPTrustPin).where(UCPTrustPin.origin == identity.origin).with_for_update()
            )
            if record is None:
                raise UCPTrustError("trust_unavailable")
            if not _matches(record, identity):
                raise UCPTrustError("trust_mismatch")
            record.last_seen_at = now
            return _pin(record)

    async def rotate_pin(
        self,
        origin: str,
        expected_fingerprint: str,
        replacement: BuyerIdentity,
        now: datetime,
    ) -> TrustPin:
        if replacement.origin != origin:
            raise UCPTrustError("trust_mismatch")
        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(UCPTrustPin).where(UCPTrustPin.origin == origin).with_for_update()
            )
            if record is None or not record.active or record.fingerprint != expected_fingerprint:
                raise UCPTrustError("trust_mismatch")
            record.profile_url = replacement.profile_url
            record.key_id = replacement.key_id
            record.fingerprint = replacement.fingerprint
            record.ucp_version = replacement.version
            record.last_seen_at = now
            rotation_digest = hashlib.sha256(
                f"{origin}\0{expected_fingerprint}\0{replacement.fingerprint}".encode()
            ).hexdigest()
            session.add(
                UCPExchangeEvent(
                    id=uuid4(),
                    method="ROTATE",
                    route="/internal/ucp/trust-pins/rotate",
                    profile_origin=origin,
                    profile_url_sha256=hashlib.sha256(replacement.profile_url.encode()).hexdigest(),
                    buyer_key_id=replacement.key_id,
                    buyer_fingerprint=replacement.fingerprint,
                    nonce_sha256=None,
                    request_sha256=rotation_digest,
                    response_sha256=None,
                    http_status=200,
                    outcome="trust_rotated",
                    checkout_id=None,
                    started_at=now,
                    completed_at=now,
                )
            )
            return _pin(record)

    async def list_trust_pins(
        self,
        *,
        limit: int,
        after: str | None,
    ) -> tuple[TrustPin, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        statement = select(UCPTrustPin).where(UCPTrustPin.active.is_(True))
        if after is not None:
            statement = statement.where(UCPTrustPin.origin > after)
        statement = statement.order_by(UCPTrustPin.origin).limit(limit)
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
            return tuple(_pin(record) for record in records)

    async def append_exchange(self, event: NewUCPExchange) -> UUID:
        event_id = uuid4()
        async with self._session_factory() as session, session.begin():
            session.add(UCPExchangeEvent(id=event_id, **asdict(event)))
        return event_id

    async def list_exchanges(
        self,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> tuple[UCPExchange, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        statement = select(UCPExchangeEvent)
        if before is not None:
            completed_at, exchange_id = before
            statement = statement.where(
                or_(
                    UCPExchangeEvent.completed_at < completed_at,
                    and_(
                        UCPExchangeEvent.completed_at == completed_at,
                        UCPExchangeEvent.id < exchange_id,
                    ),
                )
            )
        statement = statement.order_by(
            UCPExchangeEvent.completed_at.desc(), UCPExchangeEvent.id.desc()
        ).limit(limit)
        async with self._session_factory() as session:
            records = (await session.scalars(statement)).all()
            return tuple(_exchange(record) for record in records)

    async def get_exchange(self, exchange_id: UUID) -> UCPExchange | None:
        async with self._session_factory() as session:
            record = await session.get(UCPExchangeEvent, exchange_id)
            return _exchange(record) if record is not None else None


def _matches(record: UCPTrustPin, identity: BuyerIdentity) -> bool:
    return (
        record.active
        and record.profile_url == identity.profile_url
        and record.key_id == identity.key_id
        and record.fingerprint == identity.fingerprint
        and record.ucp_version == identity.version
    )


def _pin(record: UCPTrustPin) -> TrustPin:
    return TrustPin(
        origin=record.origin,
        profile_url=record.profile_url,
        key_id=record.key_id,
        fingerprint=record.fingerprint,
        version=record.ucp_version,
        first_seen_at=record.first_seen_at,
        last_seen_at=record.last_seen_at,
    )


def _exchange(record: UCPExchangeEvent) -> UCPExchange:
    return UCPExchange(
        id=record.id,
        method=record.method,
        route=record.route,
        profile_origin=record.profile_origin,
        profile_url_sha256=record.profile_url_sha256,
        buyer_key_id=record.buyer_key_id,
        buyer_fingerprint=record.buyer_fingerprint,
        nonce_sha256=record.nonce_sha256,
        request_sha256=record.request_sha256,
        response_sha256=record.response_sha256,
        http_status=record.http_status,
        outcome=record.outcome,
        checkout_id=record.checkout_id,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )
