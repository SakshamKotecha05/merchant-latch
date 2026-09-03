from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from acsa.domain.ucp_checkout import StoredCheckout


class CheckoutPersistenceOutcome(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    CONFLICT = "conflict"
    NONCE_REPLAY = "nonce_replay"


@dataclass(frozen=True, slots=True)
class CheckoutPersistenceResult:
    outcome: CheckoutPersistenceOutcome
    checkout: StoredCheckout | None


class UCPCheckoutStorePort(Protocol):
    async def create_or_replay(
        self,
        *,
        buyer_key_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        idempotency_key: str,
        request_sha256: str,
        checkout: StoredCheckout,
    ) -> CheckoutPersistenceResult: ...

    async def get(self, checkout_id: str) -> StoredCheckout | None: ...
