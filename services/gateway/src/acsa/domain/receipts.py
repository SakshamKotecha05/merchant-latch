from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderOrderCandidate:
    order_id: str
    receipt: str
    amount_minor: int
    currency: str
    notes: Mapping[str, str]


def build_receipt(
    *,
    merchant_id: str,
    payment_attempt_id: str,
    snapshot_checksum: str,
) -> str:
    material = f"{merchant_id}|{payment_attempt_id}|{snapshot_checksum}".encode()
    digest = hashlib.sha256(material).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"acsa1_{encoded[:26]}"


def select_exact_order(
    candidates: Iterable[ProviderOrderCandidate],
    *,
    expected_receipt: str,
    expected_amount_minor: int,
    expected_currency: str,
    expected_notes: Mapping[str, str],
) -> ProviderOrderCandidate | None:
    matches = [
        candidate
        for candidate in candidates
        if candidate.receipt == expected_receipt
        and candidate.amount_minor == expected_amount_minor
        and candidate.currency == expected_currency
        and dict(candidate.notes) == dict(expected_notes)
    ]
    return matches[0] if len(matches) == 1 else None
