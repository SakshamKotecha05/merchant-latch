from __future__ import annotations

from typing import Protocol
from uuid import UUID


class JobDispatcherPort(Protocol):
    async def dispatch(self, job_id: UUID) -> None: ...
