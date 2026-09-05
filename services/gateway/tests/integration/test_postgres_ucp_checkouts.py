from __future__ import annotations

from datetime import UTC, datetime

import pytest

from acsa.adapters.postgres.ucp_checkouts import PostgresUCPCheckoutStore
from acsa.domain.ucp_checkout import create_escalated_checkout
from acsa.ports.ucp_checkouts import CheckoutPersistenceOutcome

pytestmark = pytest.mark.integration


def _checkout(checkout_id: str):
    return create_escalated_checkout(
        checkout_id=checkout_id,
        buyer_key_id="buyer-p256-2026-01",
        line_items=[{"item": {"id": "sku_test"}, "quantity": 1}],
        continue_url=f"https://merchant.example/checkout/{checkout_id}",
        expires_at=datetime(2026, 9, 5, tzinfo=UTC),
    )


async def test_create_or_replay_persists_one_checkout_and_replays_original_bytes(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = PostgresUCPCheckoutStore(session_factory)
    created = await store.create_or_replay(
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-a",
        nonce_expires_at=datetime(2026, 9, 5, tzinfo=UTC),
        idempotency_key="idem-a",
        request_sha256="a" * 64,
        checkout=_checkout("chk_test_01"),
    )
    replayed = await store.create_or_replay(
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-b",
        nonce_expires_at=datetime(2026, 9, 5, tzinfo=UTC),
        idempotency_key="idem-a",
        request_sha256="a" * 64,
        checkout=_checkout("chk_test_02"),
    )

    assert created.outcome is CheckoutPersistenceOutcome.CREATED
    assert created.checkout is not None
    assert replayed.outcome is CheckoutPersistenceOutcome.REPLAYED
    assert replayed.checkout is not None
    assert replayed.checkout.id == "chk_test_01"
    assert replayed.checkout.response_body == created.checkout.response_body


async def test_nonce_reuse_for_a_distinct_operation_is_rejected(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    store = PostgresUCPCheckoutStore(session_factory)
    await store.create_or_replay(
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-a",
        nonce_expires_at=datetime(2026, 9, 5, tzinfo=UTC),
        idempotency_key="idem-a",
        request_sha256="a" * 64,
        checkout=_checkout("chk_test_01"),
    )
    result = await store.create_or_replay(
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-a",
        nonce_expires_at=datetime(2026, 9, 5, tzinfo=UTC),
        idempotency_key="idem-b",
        request_sha256="b" * 64,
        checkout=_checkout("chk_test_02"),
    )

    assert result.outcome is CheckoutPersistenceOutcome.NONCE_REPLAY
    assert result.checkout is None
