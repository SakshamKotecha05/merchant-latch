"""Provider payment evidence consumed by merchant-side finalization."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderPaymentRecord:
    payment_id: str
    order_id: str
    amount_minor: int
    currency: str
    status: str
    captured: bool
