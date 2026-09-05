from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import orjson
import pytest
from sqlalchemy import func, select

from acsa.adapters.postgres.commerce import PostgresCommerceStore
from acsa.adapters.postgres.models import (
    AuditEvent,
    CheckoutLine,
    CheckoutSession,
    CommerceIdempotencyRecord,
    Inventory,
    MerchantConfig,
    OutboxJob,
    PaymentAttempt,
    PickupLocation,
    PolicyPack,
    Product,
    UCPCheckout,
    UCPRequestNonce,
    Variant,
)
from acsa.domain.commerce import CommerceMutationOutcome, RequestedLine

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


async def _seed(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session, session.begin():
        session.add(
            MerchantConfig(
                id="merchant_demo",
                public_name="MerchantLatch",
                currency="INR",
                active_policy_pack_version=1,
            )
        )
        session.add(
            PolicyPack(
                merchant_id="merchant_demo",
                version=1,
                rules={
                    "max_quantity_per_line": 2,
                    "max_total_quantity": 3,
                    "approval_lifetime_seconds": 600,
                    "inventory_lease_lifetime_seconds": 600,
                },
            )
        )
        session.add(
            PickupLocation(
                id="pickup_blr_01",
                merchant_id="merchant_demo",
                name="MerchantLatch Bengaluru",
                city="Bengaluru",
                active=True,
            )
        )
        session.add(
            Product(
                id="prod_stride",
                merchant_id="merchant_demo",
                name="Stride One",
                description="A clean everyday sneaker.",
                search_text="stride one clean everyday sneaker",
                active=True,
            )
        )
        session.add(
            Variant(
                id="var_stride_42_black",
                product_id="prod_stride",
                sku="ML-STRIDE-BLK-42",
                size="42",
                color="Black",
                unit_price_minor=499_900,
                currency="INR",
                active=True,
            )
        )
        await session.flush()
        session.add(
            Inventory(
                variant_id="var_stride_42_black",
                on_hand=5,
                reserved=0,
                sold=0,
                version=3,
            )
        )


async def test_create_checkout_persists_authoritative_terms_and_audit(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)

    result = await store.create_checkout(
        checkout_id="chk_test_01",
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=500_000,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )

    assert result.outcome is CommerceMutationOutcome.CREATED
    assert result.checkout is not None
    assert result.checkout.pricing.total_minor == 499_900
    assert result.checkout.lines[0].unit_price_minor == 499_900
    assert result.checkout.lines[0].inventory_version == 3
    assert result.response_body == result.checkout.canonical_bytes
    resource = orjson.loads(result.checkout.canonical_bytes)
    assert resource["ucp"]["payment_handlers"] == {}
    assert resource["currency"] == "INR"
    assert resource["links"] == []
    assert [total["type"] for total in resource["totals"]] == ["subtotal", "total"]
    assert resource["line_items"][0]["id"] == "var_stride_42_black"
    assert resource["line_items"][0]["item"]["price"] == 499_900
    assert resource["line_items"][0]["totals"][-1] == {
        "type": "total",
        "amount": 499_900,
    }
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CheckoutSession)) == 1
        assert await session.scalar(select(func.count()).select_from(CheckoutLine)) == 1
        assert await session.scalar(select(func.count()).select_from(UCPCheckout)) == 1
        assert await session.scalar(select(func.count()).select_from(UCPRequestNonce)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 0


async def test_identical_create_replay_returns_original_bytes_without_second_checkout(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)
    request = dict(
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )

    created = await store.create_checkout(checkout_id="chk_test_01", **request)
    replayed = await store.create_checkout(checkout_id="chk_test_02", **request)

    assert created.outcome is CommerceMutationOutcome.CREATED
    assert replayed.outcome is CommerceMutationOutcome.REPLAYED
    assert replayed.response_body == created.response_body
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CheckoutSession)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1


async def test_lookup_idempotency_returns_none_replay_or_conflict(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)

    unused = await store.lookup_idempotency(
        buyer_key_id="buyer-p256-2026-01",
        operation="create_checkout",
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
    )
    created = await store.create_checkout(
        checkout_id="chk_test_01",
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )
    replayed = await store.lookup_idempotency(
        buyer_key_id="buyer-p256-2026-01",
        operation="create_checkout",
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
    )
    conflict = await store.lookup_idempotency(
        buyer_key_id="buyer-p256-2026-01",
        operation="create_checkout",
        idempotency_key="idem-create-01",
        request_sha256="b" * 64,
    )

    assert unused is None
    assert created.outcome is CommerceMutationOutcome.CREATED
    assert replayed is not None
    assert replayed.outcome is CommerceMutationOutcome.REPLAYED
    assert replayed.response_body == created.response_body
    assert conflict is not None
    assert conflict.outcome is CommerceMutationOutcome.CONFLICT


async def test_changed_create_replay_conflicts_without_state_change(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)
    base = dict(
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )
    await store.create_checkout(checkout_id="chk_test_01", request_sha256="a" * 64, **base)

    conflict = await store.create_checkout(
        checkout_id="chk_test_02", request_sha256="b" * 64, **base
    )

    assert conflict.outcome is CommerceMutationOutcome.CONFLICT
    assert conflict.checkout is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CheckoutSession)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 0


async def test_update_requires_exact_version_and_reprices_from_catalog(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)
    await store.create_checkout(
        checkout_id="chk_test_01",
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )
    async with session_factory() as session, session.begin():
        variant = await session.get(Variant, "var_stride_42_black")
        assert variant is not None
        variant.unit_price_minor = 519_900

    stale = await store.update_checkout(
        checkout_id="chk_test_01",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-update-stale",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=2,
        idempotency_key="idem-update-stale",
        request_sha256="b" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
    )
    updated = await store.update_checkout(
        checkout_id="chk_test_01",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-update-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=1,
        idempotency_key="idem-update-01",
        request_sha256="c" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
    )

    assert stale.outcome is CommerceMutationOutcome.STALE
    assert updated.outcome is CommerceMutationOutcome.UPDATED
    assert updated.checkout is not None
    assert updated.checkout.version == 2
    assert updated.checkout.pricing.total_minor == 519_900


async def test_cancel_is_idempotent_and_blocks_later_updates(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)
    await store.create_checkout(
        checkout_id="chk_test_01",
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )

    canceled = await store.cancel_checkout(
        checkout_id="chk_test_01",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-cancel-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=1,
        idempotency_key="idem-cancel-01",
        request_sha256="b" * 64,
    )
    replayed = await store.cancel_checkout(
        checkout_id="chk_test_01",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-cancel-replay",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=1,
        idempotency_key="idem-cancel-01",
        request_sha256="b" * 64,
    )
    blocked = await store.update_checkout(
        checkout_id="chk_test_01",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-update-after-cancel",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        expected_version=2,
        idempotency_key="idem-update-after-cancel",
        request_sha256="c" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
    )

    assert canceled.outcome is CommerceMutationOutcome.CANCELED
    assert replayed.outcome is CommerceMutationOutcome.REPLAYED
    assert replayed.response_body == canceled.response_body
    assert blocked.outcome is CommerceMutationOutcome.BLOCKED


async def test_invalid_variant_creates_no_checkout_audit_or_provider_job(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)

    blocked = await store.create_checkout(
        checkout_id="chk_test_01",
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_missing", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )

    assert blocked.outcome is CommerceMutationOutcome.BLOCKED
    assert blocked.rule_ids == ("variant_not_found",)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CheckoutSession)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(CommerceIdempotencyRecord)) == 0
        )
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxJob)) == 0
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0


async def test_reused_signature_nonce_conflicts_without_second_checkout(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)
    request = dict(
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        expires_at=NOW + timedelta(minutes=30),
    )
    await store.create_checkout(
        checkout_id="chk_test_01",
        idempotency_key="idem-create-01",
        continue_url="https://merchant.example/checkout/chk_test_01",
        **request,
    )

    conflict = await store.create_checkout(
        checkout_id="chk_test_02",
        idempotency_key="idem-create-02",
        continue_url="https://merchant.example/checkout/chk_test_02",
        **request,
    )

    assert conflict.outcome is CommerceMutationOutcome.CONFLICT
    assert conflict.rule_ids == ("nonce_replayed",)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CheckoutSession)) == 1


async def test_concurrent_updates_accept_exactly_one_checkout_version(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)
    await store.create_checkout(
        checkout_id="chk_test_01",
        merchant_id="merchant_demo",
        buyer_key_id="buyer-p256-2026-01",
        nonce="nonce-create-01",
        nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        idempotency_key="idem-create-01",
        request_sha256="a" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        pickup_location_id="pickup_blr_01",
        budget_minor=None,
        continue_url="https://merchant.example/checkout/chk_test_01",
        expires_at=NOW + timedelta(minutes=30),
    )

    async def update(suffix: str) -> CommerceMutationOutcome:
        result = await store.update_checkout(
            checkout_id="chk_test_01",
            buyer_key_id="buyer-p256-2026-01",
            nonce=f"nonce-update-{suffix}",
            nonce_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            expected_version=1,
            idempotency_key=f"idem-update-{suffix}",
            request_sha256=suffix * 64,
            requested_lines=[RequestedLine("var_stride_42_black", 1)],
            pickup_location_id="pickup_blr_01",
            budget_minor=None,
        )
        return result.outcome

    outcomes = await asyncio.gather(update("b"), update("c"))

    assert sorted(outcomes) == [CommerceMutationOutcome.STALE, CommerceMutationOutcome.UPDATED]
    async with session_factory() as session:
        checkout = await session.get(CheckoutSession, "chk_test_01")
        assert checkout is not None
        assert checkout.version == 2
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 2
