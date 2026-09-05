from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from test_browser_sessions import Store
from test_postgres_payment_finalization import NOW, _seed_finalizable_attempt, _service

from acsa.adapters.postgres.models import (
    ApprovalSnapshotRecord,
    CheckoutSession,
    InventoryLease,
    PaymentAttempt,
)
from acsa.security.browser_sessions import BrowserAuthorization
from acsa.web.payment_confirmation import create_payment_confirmation_router

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "invalid", ["lease_expired", "lease_released", "approval_expired", "canceled", "uncertain"]
)
async def test_browser_cannot_launch_invalid_payment_attempt(session_factory, invalid):
    await _seed_finalizable_attempt(session_factory)
    async with session_factory() as session, session.begin():
        lease = await session.scalar(select(InventoryLease))
        if invalid == "lease_expired":
            lease.expires_at = NOW - timedelta(seconds=1)
        elif invalid == "lease_released":
            lease.state = "released"
        elif invalid == "approval_expired":
            snapshot = await session.scalar(select(ApprovalSnapshotRecord))
            snapshot.expires_at = NOW - timedelta(seconds=1)
        elif invalid == "canceled":
            checkout = await session.get(CheckoutSession, "chk_1")
            checkout.status = "canceled"
        else:
            attempt = await session.get(PaymentAttempt, "att_1")
            attempt.provider_uncertain = True
    app = FastAPI()
    app.include_router(
        create_payment_confirmation_router(
            _service(session_factory), BrowserAuthorization(Store(), "https://merchant.example")
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://merchant.example"
    ) as browser:
        response = await browser.get(
            "/api/payments/razorpay/launch/att_1",
            headers={"Authorization": "Bearer " + "session-" * 8},
        )
    assert response.status_code == 404
    assert "order_id" not in response.json()


async def test_valid_attempt_still_launches(session_factory):
    await _seed_finalizable_attempt(session_factory)
    result = await _service(session_factory).payment_launch_configuration("att_1")
    assert result is not None
    assert result.amount_minor == 499900
