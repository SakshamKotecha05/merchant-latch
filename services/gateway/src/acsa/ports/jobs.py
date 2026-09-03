from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class JobDispatcherPort(Protocol):
    async def dispatch(self, job_id: UUID) -> None: ...


class OutboxDispatchStorePort(Protocol):
    async def mark_dispatched(self, job_id: UUID) -> bool: ...


class OutboxSweepStorePort(Protocol):
    async def pending_dispatch_ids(self, *, limit: int) -> list[UUID]: ...


class ClaimedJobPort(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def job_type(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, Any]: ...


class OutboxClaimState(StrEnum):
    CLAIMED = "claimed"
    LEASED = "leased"
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class OutboxClaimResult:
    state: OutboxClaimState
    job: ClaimedJobPort | None = None


class OutboxWorkerStorePort(Protocol):
    async def claim(self, *, job_id: UUID, worker_id: str) -> OutboxClaimResult: ...

    async def complete(self, *, job_id: UUID, worker_id: str) -> bool: ...
