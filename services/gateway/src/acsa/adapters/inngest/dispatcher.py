from __future__ import annotations

from typing import Protocol
from uuid import UUID

import inngest


class InngestSender(Protocol):
    async def send(self, events: inngest.Event | list[inngest.Event]) -> list[str]: ...


class InngestJobDispatcher:
    def __init__(self, client: InngestSender) -> None:
        self._client = client

    async def dispatch(self, job_id: UUID) -> None:
        event = inngest.Event(
            name="acsa/outbox.ready",
            id=str(job_id),
            data={"job_id": str(job_id)},
        )
        await self._client.send(event)
