from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fixture_keys import merchant_private_key
from test_browser_sessions import Store

from acsa.domain.canonical import canonical_json_bytes, sha256_checksum
from acsa.domain.commerce import (
    ApprovalOutcome,
    ApprovalPreview,
    ApprovalResult,
    ApprovalSnapshot,
)
from acsa.security.browser_sessions import BrowserAuthorization
from acsa.security.continue_tokens import issue_continue_token
from acsa.services.commerce import CommerceService
from acsa.web.merchant_checkout import create_merchant_checkout_router

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
OUTBOX_JOB_ID = UUID("12345678-1234-5678-1234-567812345678")


@pytest.mark.asyncio
async def test_approval_service_preserves_version_checksum_and_idempotency_guards() -> None:
    store = AsyncMock()
    store.preview_approval.return_value = ApprovalPreview(ApprovalOutcome.READY)
    store.approve_checkout.return_value = ApprovalResult(ApprovalOutcome.APPROVED)
    service = CommerceService(
        store=store,
        merchant_id="merchant_demo",
        pickup_location_id="pickup_blr_01",
        public_merchant_url="https://merchant.example",
    )

    await service.preview_approval(
        checkout_id="chk_1",
        expected_version=1,
        approved_at=NOW,
    )
    await service.approve_checkout(
        checkout_id="chk_1",
        expected_version=1,
        snapshot_checksum="a" * 64,
        idempotency_key="idem-approve",
        request_sha256="b" * 64,
        approved_at=NOW,
    )

    store.preview_approval.assert_awaited_once_with(
        checkout_id="chk_1", expected_version=1, approved_at=NOW
    )
    store.approve_checkout.assert_awaited_once_with(
        checkout_id="chk_1",
        expected_version=1,
        snapshot_checksum="a" * 64,
        idempotency_key="idem-approve",
        request_sha256="b" * 64,
        approved_at=NOW,
    )


def _private_key() -> ec.EllipticCurvePrivateKey:
    return merchant_private_key()


class ApprovalServiceStub:
    def __init__(self) -> None:
        resource = {"checkoutId": "chk_1", "checkoutVersion": 1, "totalMinor": 499_900}
        self.snapshot = ApprovalSnapshot(
            resource=resource,
            canonical_bytes=canonical_json_bytes(resource),
            checksum=sha256_checksum(resource),
            expires_at=NOW + timedelta(minutes=10),
        )

    async def preview_approval(self, **_: object) -> ApprovalPreview:
        return ApprovalPreview(ApprovalOutcome.READY, snapshot=self.snapshot)

    async def approve_checkout(self, **_: object) -> ApprovalResult:
        body = canonical_json_bytes({"status": "approved", "attempt": {"id": "att_1"}})
        return ApprovalResult(
            ApprovalOutcome.APPROVED,
            attempt_id="att_1",
            response_body=body,
            outbox_job_id=OUTBOX_JOB_ID,
        )


def _route_client(
    dispatcher: AsyncMock | None = None,
) -> tuple[TestClient, ApprovalServiceStub, str]:
    private_key = _private_key()
    service = ApprovalServiceStub()
    token = issue_continue_token(
        private_key,
        checkout_id="chk_1",
        checkout_version=1,
        now=NOW,
    )
    app = FastAPI()
    app.include_router(
        create_merchant_checkout_router(
            commerce_service=service,
            merchant_public_key=private_key.public_key(),
            clock=lambda: NOW,
            authorization=BrowserAuthorization(Store(), "https://merchant.example"),
            job_dispatcher=dispatcher or AsyncMock(),
        )
    )
    return (
        TestClient(
            app,
            headers={
                "Authorization": "Bearer " + "session-" * 8,
                "Origin": "https://merchant.example",
                "X-CSRF-Token": "csrf-" * 8,
            },
        ),
        service,
        token,
    )


def test_review_and_approval_routes_require_bound_continue_session() -> None:
    client, service, token = _route_client()

    review = client.get(
        "/api/checkouts/chk_1/review",
        params={"version": 1, "session": token},
    )
    approval = client.post(
        "/api/checkouts/chk_1/approve",
        headers={"Idempotency-Key": "idem-approve-01"},
        content=json.dumps(
            {
                "confirmed": True,
                "snapshot_checksum": service.snapshot.checksum,
            }
        ),
    )

    assert review.status_code == 200
    assert review.json()["snapshot_checksum"] == service.snapshot.checksum
    assert approval.status_code == 200
    assert approval.json() == {"attempt": {"id": "att_1"}, "status": "approved"}


def test_review_route_rejects_tampered_session_without_details() -> None:
    client, _, token = _route_client()
    client.headers.pop("authorization")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    response = client.get(
        "/api/checkouts/chk_1/review",
        params={"version": 1, "session": tampered},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "merchant_session_required"}


def test_fresh_approval_dispatches_provider_order_job_immediately() -> None:
    dispatcher = AsyncMock()
    client, service, _ = _route_client(dispatcher)

    response = client.post(
        "/api/checkouts/chk_1/approve",
        headers={"Idempotency-Key": "idem-approve-01"},
        content=json.dumps(
            {
                "confirmed": True,
                "snapshot_checksum": service.snapshot.checksum,
            }
        ),
    )

    assert response.status_code == 200
    dispatcher.dispatch.assert_awaited_once_with(OUTBOX_JOB_ID)


def test_dispatch_failure_keeps_approval_successful_for_sweep_recovery() -> None:
    dispatcher = AsyncMock()
    dispatcher.dispatch.side_effect = RuntimeError("Inngest unavailable")
    client, service, _ = _route_client(dispatcher)

    response = client.post(
        "/api/checkouts/chk_1/approve",
        headers={"Idempotency-Key": "idem-approve-01"},
        content=json.dumps(
            {
                "confirmed": True,
                "snapshot_checksum": service.snapshot.checksum,
            }
        ),
    )

    assert response.status_code == 200
    assert response.json() == {"attempt": {"id": "att_1"}, "status": "approved"}
    dispatcher.dispatch.assert_awaited_once_with(OUTBOX_JOB_ID)
