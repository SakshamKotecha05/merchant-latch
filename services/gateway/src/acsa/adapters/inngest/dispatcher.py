from __future__ import annotations

from typing import Protocol
from uuid import UUID

import inngest

from acsa.ports.jobs import OutboxDispatchStorePort


class InngestSender(Protocol):
    async def send(self, events: inngest.Event | list[inngest.Event]) -> list[str]: ...


class InngestJobDispatcher:
    def __init__(self, client: InngestSender, outbox_store: OutboxDispatchStorePort) -> None:
        self._client = client
        self._outbox_store = outbox_store

    async def dispatch(self, job_id: UUID) -> None:
        event = inngest.Event(
            name="acsa/outbox.ready",
            id=str(job_id),
            data={"job_id": str(job_id)},
        )
        await self._client.send(event)
        await self._outbox_store.mark_dispatched(job_id)
