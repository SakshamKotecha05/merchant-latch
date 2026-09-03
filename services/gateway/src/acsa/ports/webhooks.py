from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WebhookInsertResult:
    created: bool
    job_id: UUID | None


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
    async def mark_processed(self, webhook_event_id: UUID) -> bool: ...
