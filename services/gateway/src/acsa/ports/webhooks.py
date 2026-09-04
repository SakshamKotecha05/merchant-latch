from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WebhookInsertResult:
    created: bool
    job_id: UUID | None


@dataclass(frozen=True, slots=True)
class WebhookFinalizationWork:
    attempt_id: str
    payment_id: str
    order_id: str


class WebhookStorePort(Protocol):
    async def insert_verified_event(
        self,
        *,
        event_id: str,
        event_name: str,
        raw_payload: bytes,
        payload_hash: str,
    ) -> WebhookInsertResult: ...


class WebhookProcessingStorePort(Protocol):
    async def load_finalization_work(
        self, webhook_event_id: UUID
    ) -> WebhookFinalizationWork | None: ...

    async def mark_processed(self, webhook_event_id: UUID) -> bool: ...
