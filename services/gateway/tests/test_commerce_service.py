from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acsa.domain.commerce import CommerceMutationOutcome, CommerceMutationResult, RequestedLine
from acsa.services.commerce import CommerceService

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_lookup_idempotency_preserves_the_store_contract() -> None:
    store = AsyncMock()
    expected = CommerceMutationResult(CommerceMutationOutcome.REPLAYED)
    store.lookup_idempotency.return_value = expected
    service = CommerceService(
        store=store,
        merchant_id="merchant_demo",
        pickup_location_id="pickup_blr_01",
        public_merchant_url="https://merchant.example",
    )

    result = await service.lookup_idempotency(
        buyer_key_id="buyer-1",
        operation="create_checkout",
        idempotency_key="idem-1",
        request_sha256="a" * 64,
    )

    assert result is expected
    store.lookup_idempotency.assert_awaited_once_with(
        buyer_key_id="buyer-1",
        operation="create_checkout",
        idempotency_key="idem-1",
        request_sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_create_checkout_supplies_server_owned_identity_and_expiry() -> None:
    store = AsyncMock()
    store.create_checkout.return_value = CommerceMutationResult(CommerceMutationOutcome.CREATED)
    service = CommerceService(
        store=store,
        merchant_id="merchant_demo",
        pickup_location_id="pickup_blr_01",
        public_merchant_url="https://merchant.example",
        clock=lambda: NOW,
        checkout_id_factory=lambda: "chk_fixed",
        continue_token_issuer=lambda checkout_id, version, now: (
            f"token-{checkout_id}-{version}-{int(now.timestamp())}"
        ),
    )

    result = await service.create_checkout(
        buyer_key_id="buyer-1",
        nonce="nonce-1",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-1",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        budget_minor=500_000,
    )

    assert result.outcome is CommerceMutationOutcome.CREATED
    store.create_checkout.assert_awaited_once_with(
        checkout_id="chk_fixed",
        merchant_id="merchant_demo",
        buyer_key_id="buyer-1",
        nonce="nonce-1",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-1",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=500_000,
        continue_url=(
            "https://merchant.example/checkout/chk_fixed?version=1&"
            "session=token-chk_fixed-1-1788523200"
        ),
        expires_at=datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_update_and_cancel_preserve_buyer_and_request_guards() -> None:
    store = AsyncMock()
    store.update_checkout.return_value = CommerceMutationResult(CommerceMutationOutcome.UPDATED)
    store.cancel_checkout.return_value = CommerceMutationResult(CommerceMutationOutcome.CANCELED)
    service = CommerceService(
        store=store,
        merchant_id="merchant_demo",
        pickup_location_id="pickup_blr_01",
        public_merchant_url="https://merchant.example/",
    )

    await service.update_checkout(
        checkout_id="chk_1",
        buyer_key_id="buyer-1",
        nonce="nonce-update",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=2,
        idempotency_key="idem-update",
        request_sha256="b" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 2)],
        budget_minor=None,
    )
    await service.cancel_checkout(
        checkout_id="chk_1",
        buyer_key_id="buyer-1",
        nonce="nonce-cancel",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=3,
        idempotency_key="idem-cancel",
        request_sha256="c" * 64,
    )

    store.update_checkout.assert_awaited_once_with(
        checkout_id="chk_1",
        buyer_key_id="buyer-1",
        nonce="nonce-update",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=2,
        idempotency_key="idem-update",
        request_sha256="b" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 2)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_1",
    )
    store.cancel_checkout.assert_awaited_once_with(
        checkout_id="chk_1",
        buyer_key_id="buyer-1",
        nonce="nonce-cancel",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=3,
        idempotency_key="idem-cancel",
        request_sha256="c" * 64,
    )
