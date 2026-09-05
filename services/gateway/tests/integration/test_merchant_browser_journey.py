import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import test_postgres_approval as fixture
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from sqlalchemy import func, select

from acsa.adapters.postgres.browser_sessions import PostgresBrowserSessionStore
from acsa.adapters.postgres.models import Inventory, MerchantBrowserSession, PaymentAttempt
from acsa.security.browser_sessions import BrowserAuthorization
from acsa.security.continue_tokens import issue_continue_token
from acsa.services.commerce import CommerceService
from acsa.web.merchant_checkout import create_merchant_checkout_router
from acsa.web.merchant_sessions import create_merchant_session_router

pytestmark = pytest.mark.integration


async def setup_journey(session_factory, monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(fixture, "NOW", now)
    commerce = await fixture._seed_checkout(session_factory)
    store = PostgresBrowserSessionStore(session_factory)
    auth = BrowserAuthorization(store, "https://merchant.example")
    key = ec.generate_private_key(ec.SECP256R1())
    app = FastAPI()
    app.include_router(create_merchant_session_router(store, auth, key.public_key()))
    service = CommerceService(
        store=commerce,
        merchant_id="merchant_demo",
        pickup_location_id="pickup_blr_01",
        public_merchant_url="https://merchant.example",
    )
    app.include_router(
        create_merchant_checkout_router(
            commerce_service=service, merchant_public_key=key.public_key(), authorization=auth
        )
    )
    token = issue_continue_token(key, checkout_id="chk_test_01", checkout_version=1, now=now)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://merchant.example",
        headers={"Origin": "https://merchant.example"},
    )
    return client, {"checkout_id": "chk_test_01", "version": 1, "continuation": token}


async def test_http_review_consent_approval_and_duplicate_have_one_attempt(
    session_factory, monkeypatch
):
    client, exchange = await setup_journey(session_factory, monkeypatch)
    async with client:
        response = await client.post("/api/merchant/session", json=exchange)
        assert response.status_code == 200
        credentials = response.json()
        client.headers.update(
            {
                "Authorization": "Bearer " + credentials["session"],
                "X-CSRF-Token": credentials["csrf"],
            }
        )
        review = await client.get("/api/checkouts/chk_test_01/review")
        assert review.status_code == 200
        assert review.json()["snapshot"]["pricing"]["totalMinor"] == 499900
        body = {"snapshot_checksum": review.json()["snapshot_checksum"], "confirmed": True}
        path = "/api/checkouts/chk_test_01/approve"
        no_consent = await client.post(
            path, json={**body, "confirmed": False}, headers={"Idempotency-Key": "one"}
        )
        assert no_consent.status_code == 400
        bad_origin = await client.post(
            path, json=body, headers={"Origin": "https://evil.example", "Idempotency-Key": "one"}
        )
        assert bad_origin.status_code == 403
        approved = await client.post(path, json=body, headers={"Idempotency-Key": "one"})
        replay = await client.post(path, json=body, headers={"Idempotency-Key": "one"})
        assert approved.status_code == replay.status_code == 200
        assert approved.json() == replay.json()
        status = await client.get("/api/checkouts/chk_test_01/status")
        assert status.json()["attempt"]["id"] == approved.json()["attempt"]["id"]
        assert (
            next(
                event for event in status.json()["events"] if event["type"] == "checkout.approved"
            )["source"]
            == "human_browser"
        )
        assert (await client.get("/api/checkouts/chk_other/status")).status_code == 404
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1
        stock = await session.scalar(select(Inventory))
        assert stock.reserved == 1
        row = await session.scalar(select(MerchantBrowserSession))
        assert row.token_digest != credentials["session"]


async def test_concurrent_continuation_redemption_is_single_use(session_factory, monkeypatch):
    client, exchange = await setup_journey(session_factory, monkeypatch)
    async with client:
        responses = await asyncio.gather(
            *[client.post("/api/merchant/session", json=exchange) for _ in range(2)]
        )
    assert sorted(response.status_code for response in responses) == [200, 409]


async def test_expired_browser_session_cannot_read_checkout(session_factory, monkeypatch):
    client, exchange = await setup_journey(session_factory, monkeypatch)
    async with client:
        credentials = (await client.post("/api/merchant/session", json=exchange)).json()
        async with session_factory() as session, session.begin():
            record = await session.scalar(select(MerchantBrowserSession))
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        response = await client.get(
            "/api/checkouts/chk_test_01/status",
            headers={"Authorization": "Bearer " + credentials["session"]},
        )
        assert response.status_code == 401


async def test_updated_checkout_can_exchange_new_version_link(session_factory, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from acsa.adapters.postgres.commerce import PostgresCommerceStore
    from acsa.domain.commerce import RequestedLine

    client, _ = await setup_journey(session_factory, monkeypatch)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    service = CommerceService(
        store=PostgresCommerceStore(session_factory),
        merchant_id="merchant_demo",
        pickup_location_id="pickup_blr_01",
        public_merchant_url="https://merchant.example",
        continue_token_issuer=lambda checkout_id, version, issued: issue_continue_token(
            key, checkout_id=checkout_id, checkout_version=version, now=issued
        ),
    )
    result = await service.update_checkout(
        checkout_id="chk_test_01",
        buyer_key_id="buyer-p256-2026-01",
        nonce="update-link",
        nonce_expires_at=now + timedelta(minutes=10),
        expected_version=1,
        idempotency_key="update-link",
        request_sha256="e" * 64,
        requested_lines=[RequestedLine("var_stride_42_black", 1)],
        budget_minor=None,
    )
    assert result.checkout is not None
    query = parse_qs(urlsplit(result.checkout.continue_url).query)
    assert query.get("version") == ["2"]
    store = PostgresBrowserSessionStore(session_factory)
    app = FastAPI()
    app.include_router(
        create_merchant_session_router(
            store, BrowserAuthorization(store, "https://merchant.example"), key.public_key()
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://merchant.example"
    ) as browser:
        response = await browser.post(
            "/api/merchant/session",
            headers={"Origin": "https://merchant.example"},
            json={"checkout_id": "chk_test_01", "version": 2, "continuation": query["session"][0]},
        )
    await client.aclose()
    assert response.status_code == 200
