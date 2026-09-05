from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from acsa.adapters.postgres.models import (
    ApprovalSnapshotRecord,
    AuditEvent,
    Base,
    CheckoutSession,
    MerchantOrder,
    PaymentAttempt,
    UCPExchangeEvent,
)
from acsa.services.evidence import append_audit_event

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


async def test_append_audit_event_redacts_secrets_before_storage(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    async with session_factory() as session, session.begin():
        await append_audit_event(
            session,
            aggregate_type="payment_attempt",
            aggregate_id="att_1",
            event_type="payment_attempt.provider_failed",
            payload={
                "error_code": "provider_timeout",
                "authorization": "Bearer private",
                "nested": {
                    "api_key": "private",
                    "private_key": "private",
                    "safe": "value",
                    "signing_key": "private",
                },
            },
            evidence_source="system",
        )

    async with session_factory() as session:
        event = await session.scalar(select(AuditEvent))

    assert event is not None
    assert event.sequence == 1
    assert event.payload == {
        "authorization": "[REDACTED]",
        "error_code": "provider_timeout",
        "nested": {
            "api_key": "[REDACTED]",
            "private_key": "[REDACTED]",
            "safe": "value",
            "signing_key": "[REDACTED]",
        },
    }


def test_runtime_role_cannot_update_or_delete_append_only_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_url = os.environ.get("TEST_DATABASE_DIRECT_URL")
    runtime_url = os.environ.get("TEST_DATABASE_URL")
    if not owner_url or not runtime_url:
        pytest.skip("TEST_DATABASE_URL and TEST_DATABASE_DIRECT_URL are not configured")
    gateway_root = Path(__file__).parents[2]
    monkeypatch.chdir(gateway_root)
    monkeypatch.setenv("DATABASE_DIRECT_URL", owner_url)
    alembic_config = Config(str(gateway_root / "alembic.ini"))
    owner_engine = create_engine(owner_url)
    runtime_engine = create_engine(runtime_url)
    with owner_engine.begin() as connection:
        Base.metadata.drop_all(connection)
        connection.execute(
            text("DROP FUNCTION IF EXISTS reject_phase2_append_only_mutation() CASCADE")
        )
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    command.upgrade(alembic_config, "head")

    try:
        with Session(owner_engine) as session, session.begin():
            checkout = CheckoutSession(
                id="chk_append_only",
                merchant_id="merchant_demo",
                buyer_key_id="buyer_1",
                status="completed",
                version=1,
                policy_pack_version=1,
                pickup_location_id="pickup_blr_01",
                currency="INR",
                expires_at=NOW + timedelta(minutes=10),
            )
            session.add(checkout)
            session.flush()
            snapshot = ApprovalSnapshotRecord(
                checkout_id=checkout.id,
                checkout_version=1,
                policy_pack_version=1,
                checksum="a" * 64,
                canonical_body=b"{}",
                approved_by="buyer_1",
                approved_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
            session.add(snapshot)
            session.flush()
            attempt = PaymentAttempt(
                id="att_append_only",
                checkout_id=checkout.id,
                attempt_version=1,
                state="paid",
                receipt="acsa1_append_only",
                snapshot_id=snapshot.id,
                snapshot_checksum=snapshot.checksum,
                amount_minor=499_900,
                currency="INR",
                provider_uncertain=False,
                provider_account_id="rzp_test_account",
                provider_payment_id="pay_append_only",
            )
            session.add(attempt)
            session.flush()
            session.add(
                MerchantOrder(
                    checkout_id=checkout.id,
                    attempt_id=attempt.id,
                    provider_account_id="rzp_test_account",
                    provider_order_id="order_append_only",
                    provider_payment_id="pay_append_only",
                    amount_minor=499_900,
                    currency="INR",
                    evidence_digest="b" * 64,
                    evidence_source="provider_fetch",
                )
            )
            session.add(
                AuditEvent(
                    aggregate_type="payment_attempt",
                    aggregate_id=attempt.id,
                    sequence=1,
                    event_type="payment_attempt.paid",
                    payload={"status": "captured"},
                    evidence_source="provider_fetch",
                )
            )
            session.add(
                UCPExchangeEvent(
                    method="POST",
                    route="/ucp/shopping/checkout-sessions",
                    profile_origin="https://buyer.example",
                    profile_url_sha256="c" * 64,
                    buyer_key_id="buyer_1",
                    buyer_fingerprint="d" * 64,
                    nonce_sha256="e" * 64,
                    request_sha256="f" * 64,
                    response_sha256="0" * 64,
                    http_status=201,
                    outcome="accepted",
                    checkout_id=checkout.id,
                    started_at=NOW,
                    completed_at=NOW,
                )
            )

        statements = (
            "UPDATE approval_snapshots SET approved_by = 'changed'",
            "DELETE FROM approval_snapshots",
            "UPDATE merchant_orders SET evidence_source = 'changed'",
            "DELETE FROM merchant_orders",
            "UPDATE audit_events SET evidence_source = 'changed'",
            "DELETE FROM audit_events",
            "UPDATE ucp_exchange_events SET outcome = 'changed'",
            "DELETE FROM ucp_exchange_events",
        )
        for statement in statements:
            with runtime_engine.connect() as connection:
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement))
                    connection.commit()
                connection.rollback()

        with Session(owner_engine) as session:
            assert session.scalar(select(func.count()).select_from(ApprovalSnapshotRecord)) == 1
            assert session.scalar(select(func.count()).select_from(MerchantOrder)) == 1
            assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
            assert session.scalar(select(func.count()).select_from(UCPExchangeEvent)) == 1
    finally:
        runtime_engine.dispose()
        owner_engine.dispose()
