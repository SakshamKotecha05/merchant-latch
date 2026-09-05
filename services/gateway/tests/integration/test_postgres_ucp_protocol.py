from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta

import pytest
from fixture_keys import buyer_private_key
from sqlalchemy.exc import IntegrityError

from acsa.security.ucp_signatures import export_public_jwk
from acsa.ucp_profiles import BuyerIdentity, validate_profile_document

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


def _module():  # type: ignore[no-untyped-def]
    assert importlib.util.find_spec("acsa.adapters.postgres.ucp_protocol") is not None
    from acsa.adapters.postgres import ucp_protocol

    return ucp_protocol


def _identity(key_id: str = "buyer-p256-2026-01") -> BuyerIdentity:
    key = buyer_private_key()
    return validate_profile_document(
        "https://buyer.example/.well-known/ucp",
        {
            "ucp": {
                "version": "2026-04-08",
                "services": {},
                "payment_handlers": {},
            },
            "signing_keys": [export_public_jwk(key.public_key(), key_id=key_id)],
        },
        key_id,
    )


async def test_pins_first_valid_identity_and_refreshes_only_an_exact_match(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    protocol = _module()
    store = protocol.PostgresUCPProtocolStore(session_factory)
    identity = _identity()

    first = await store.verify_or_pin(identity, NOW)
    refreshed = await store.verify_or_pin(identity, NOW + timedelta(minutes=1))

    assert first.origin == "https://buyer.example"
    assert first.fingerprint == identity.fingerprint
    assert first.first_seen_at == NOW
    assert refreshed.first_seen_at == NOW
    assert refreshed.last_seen_at == NOW + timedelta(minutes=1)


async def test_rejects_an_unexpected_key_change(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    protocol = _module()
    store = protocol.PostgresUCPProtocolStore(session_factory)
    identity = _identity()
    await store.verify_or_pin(identity, NOW)
    changed = BuyerIdentity(
        profile_url=identity.profile_url,
        origin=identity.origin,
        key_id=identity.key_id,
        fingerprint="f" * 64,
        version=identity.version,
        public_key=identity.public_key,
    )

    with pytest.raises(protocol.UCPTrustError) as caught:
        await store.verify_or_pin(changed, NOW + timedelta(minutes=1))

    assert caught.value.code == "trust_mismatch"


async def test_concurrent_conflicting_first_use_allows_only_one_identity(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    protocol = _module()
    store = protocol.PostgresUCPProtocolStore(session_factory)
    identity = _identity()
    conflicting = BuyerIdentity(
        profile_url=identity.profile_url,
        origin=identity.origin,
        key_id=identity.key_id,
        fingerprint="1" * 64,
        version=identity.version,
        public_key=identity.public_key,
    )

    results = await asyncio.gather(
        store.verify_or_pin(identity, NOW),
        store.verify_or_pin(conflicting, NOW),
        return_exceptions=True,
    )

    assert sum(isinstance(result, protocol.TrustPin) for result in results) == 1
    assert sum(isinstance(result, protocol.UCPTrustError) for result in results) == 1


async def test_rotation_requires_the_current_fingerprint(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    protocol = _module()
    store = protocol.PostgresUCPProtocolStore(session_factory)
    identity = _identity()
    await store.verify_or_pin(identity, NOW)
    replacement = BuyerIdentity(
        profile_url=identity.profile_url,
        origin=identity.origin,
        key_id="buyer-p256-2026-02",
        fingerprint="e" * 64,
        version=identity.version,
        public_key=identity.public_key,
    )

    with pytest.raises(protocol.UCPTrustError):
        await store.rotate_pin(identity.origin, "0" * 64, replacement, NOW)

    rotated = await store.rotate_pin(
        identity.origin,
        identity.fingerprint,
        replacement,
        NOW + timedelta(minutes=2),
    )
    assert rotated.key_id == "buyer-p256-2026-02"
    assert rotated.fingerprint == "e" * 64
    exchanges = await store.list_exchanges(limit=25, before=None)
    assert len(exchanges) == 1
    assert exchanges[0].outcome == "trust_rotated"
    assert exchanges[0].profile_origin == identity.origin
    assert exchanges[0].buyer_fingerprint == replacement.fingerprint


async def test_exchange_ledger_returns_newest_first_redacted_records(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    protocol = _module()
    store = protocol.PostgresUCPProtocolStore(session_factory)
    first_id = await store.append_exchange(
        protocol.NewUCPExchange(
            method="POST",
            route="/ucp/shopping/checkout-sessions",
            profile_origin="https://buyer.example",
            profile_url_sha256="a" * 64,
            buyer_key_id="buyer-p256-2026-01",
            buyer_fingerprint="b" * 64,
            nonce_sha256="c" * 64,
            request_sha256="d" * 64,
            response_sha256="e" * 64,
            http_status=201,
            outcome="accepted",
            checkout_id="chk_1",
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
        )
    )
    second_id = await store.append_exchange(
        protocol.NewUCPExchange(
            method="POST",
            route="/ucp/shopping/checkout-sessions",
            profile_origin=None,
            profile_url_sha256=None,
            buyer_key_id=None,
            buyer_fingerprint=None,
            nonce_sha256=None,
            request_sha256="f" * 64,
            response_sha256=None,
            http_status=401,
            outcome="signature_rejected",
            checkout_id=None,
            started_at=NOW + timedelta(seconds=2),
            completed_at=NOW + timedelta(seconds=3),
        )
    )

    page = await store.list_exchanges(limit=25, before=None)

    assert [item.id for item in page] == [second_id, first_id]
    assert page[1].profile_origin == "https://buyer.example"
    assert not hasattr(page[1], "request_body")
    assert not hasattr(page[1], "signature")
    assert await store.get_exchange(first_id) == page[1]


async def test_exchange_ledger_cursor_does_not_skip_equal_timestamps(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    protocol = _module()
    store = protocol.PostgresUCPProtocolStore(session_factory)

    async def append(request_sha256: str) -> object:
        return await store.append_exchange(
            protocol.NewUCPExchange(
                method="GET",
                route="/ucp/shopping/checkout-sessions/chk_1",
                profile_origin="https://buyer.example",
                profile_url_sha256="a" * 64,
                buyer_key_id="buyer-p256-2026-01",
                buyer_fingerprint="b" * 64,
                nonce_sha256="c" * 64,
                request_sha256=request_sha256,
                response_sha256="e" * 64,
                http_status=200,
                outcome="accepted",
                checkout_id="chk_1",
                started_at=NOW,
                completed_at=NOW,
            )
        )

    inserted = {await append("1" * 64), await append("2" * 64)}
    first_page = await store.list_exchanges(limit=1, before=None)
    second_page = await store.list_exchanges(
        limit=1,
        before=(first_page[0].completed_at, first_page[0].id),
    )

    assert {first_page[0].id, second_page[0].id} == inserted


async def test_exchange_ledger_rejects_unknown_outcomes(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    protocol = _module()
    store = protocol.PostgresUCPProtocolStore(session_factory)

    with pytest.raises(IntegrityError):
        await store.append_exchange(
            protocol.NewUCPExchange(
                method="POST",
                route="/ucp/shopping/checkout-sessions",
                profile_origin=None,
                profile_url_sha256=None,
                buyer_key_id=None,
                buyer_fingerprint=None,
                nonce_sha256=None,
                request_sha256="f" * 64,
                response_sha256=None,
                http_status=401,
                outcome="unbounded_unknown_outcome",
                checkout_id=None,
                started_at=NOW,
                completed_at=NOW,
            )
        )
