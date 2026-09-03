from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import OutboxJob
from acsa.ports.jobs import OutboxClaimResult, OutboxClaimState


@dataclass(frozen=True, slots=True)
class ClaimedOutboxJob:
    id: UUID
    job_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    attempt_count: int


class PostgresOutboxStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        *,
        job_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> UUID:
        job_id = uuid4()
        async with self._session_factory() as session, session.begin():
            session.add(
                OutboxJob(
                    id=job_id,
                    job_type=job_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    payload=payload,
                )
            )
        return job_id

    async def pending_dispatch_ids(self, *, limit: int) -> list[UUID]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(OutboxJob.id)
                .where(
                    OutboxJob.dispatched_at.is_(None),
                    OutboxJob.completed_at.is_(None),
                    OutboxJob.dead_lettered_at.is_(None),
                    OutboxJob.available_at <= func.now(),
                )
                .order_by(OutboxJob.available_at, OutboxJob.id)
                .limit(limit)
            )
            return list(result)

    async def mark_dispatched(self, job_id: UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(OutboxJob)
                .where(
                    OutboxJob.id == job_id,
                    OutboxJob.dispatched_at.is_(None),
                    OutboxJob.completed_at.is_(None),
                    OutboxJob.dead_lettered_at.is_(None),
                )
                .values(dispatched_at=datetime.now(UTC))
                .returning(OutboxJob.id)
            )
            return result.scalar_one_or_none() is not None

    async def claim(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> OutboxClaimResult:
        async with self._session_factory() as session, session.begin():
            now = cast(datetime, await session.scalar(select(func.now())))
            job = await session.scalar(
                select(OutboxJob)
                .where(
                    OutboxJob.id == job_id,
                )
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return OutboxClaimResult(OutboxClaimState.UNAVAILABLE)
            if job.completed_at is not None:
                return OutboxClaimResult(OutboxClaimState.COMPLETED)
            if (
                job.dead_lettered_at is not None
                or job.available_at > now
                or job.attempt_count >= job.max_attempts
            ):
                return OutboxClaimResult(OutboxClaimState.UNAVAILABLE)
            if job.lock_expires_at is not None and job.lock_expires_at > now:
                return OutboxClaimResult(OutboxClaimState.LEASED)
            job.locked_by = worker_id
            job.lock_expires_at = now + timedelta(seconds=lease_seconds)
            job.attempt_count += 1
            claim = ClaimedOutboxJob(
                id=job.id,
                job_type=job.job_type,
                aggregate_type=job.aggregate_type,
                aggregate_id=job.aggregate_id,
                payload=dict(job.payload),
                attempt_count=job.attempt_count,
            )
        return OutboxClaimResult(OutboxClaimState.CLAIMED, claim)

    async def complete(self, *, job_id: UUID, worker_id: str) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(OutboxJob)
                .where(
                    OutboxJob.id == job_id,
                    OutboxJob.locked_by == worker_id,
                    OutboxJob.completed_at.is_(None),
                    OutboxJob.dead_lettered_at.is_(None),
                )
                .values(
                    completed_at=datetime.now(UTC),
                    locked_by=None,
                    lock_expires_at=None,
                )
                .returning(OutboxJob.id)
            )
            return result.scalar_one_or_none() is not None
