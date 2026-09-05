from __future__ import annotations

from uuid import UUID, uuid4

import inngest
import pytest

from acsa.adapters.inngest.dispatcher import InngestJobDispatcher


class RecordingInngestClient:
    def __init__(self) -> None:
        self.events: list[inngest.Event] = []

    async def send(self, events: inngest.Event | list[inngest.Event]) -> list[str]:
        event_list = events if isinstance(events, list) else [events]
        self.events.extend(event_list)
        return ["inngest_event_fixture"]


class RecordingOutboxStore:
    def __init__(self) -> None:
        self.marked_job_ids: list[UUID] = []

    async def mark_dispatched(self, job_id: UUID) -> bool:
        self.marked_job_ids.append(job_id)
        return True


class FailingInngestClient:
    async def send(self, events: inngest.Event | list[inngest.Event]) -> list[str]:
        raise TimeoutError("Inngest did not accept the event")


@pytest.mark.asyncio
async def test_dispatch_sends_only_deterministic_job_routing_data() -> None:
    client = RecordingInngestClient()
    outbox_store = RecordingOutboxStore()
    dispatcher = InngestJobDispatcher(client, outbox_store)
    job_id = uuid4()

    await dispatcher.dispatch(job_id)

    assert len(client.events) == 1
    event = client.events[0]
    assert event.name == "acsa/outbox.ready"
    assert event.id == str(job_id)
    assert event.data == {"job_id": str(job_id)}
    assert outbox_store.marked_job_ids == [job_id]


@pytest.mark.asyncio
async def test_dispatch_leaves_the_job_undispatched_when_inngest_rejects_the_event() -> None:
    outbox_store = RecordingOutboxStore()
    dispatcher = InngestJobDispatcher(FailingInngestClient(), outbox_store)

    with pytest.raises(TimeoutError, match="Inngest did not accept the event"):
        await dispatcher.dispatch(uuid4())

    assert outbox_store.marked_job_ids == []
