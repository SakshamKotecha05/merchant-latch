from __future__ import annotations

from uuid import UUID

import inngest
import pytest
from fastapi.testclient import TestClient

from acsa.application import create_application
from acsa.inngest_functions import create_outbox_ready_function, create_outbox_sweep_function
from acsa.ports.jobs import OutboxClaimResult, OutboxClaimState
from acsa.ports.webhooks import WebhookInsertResult


class NoopStore:
    async def insert_verified_event(
        self,
        *,
        event_id: str,
        event_name: str,
        raw_payload: bytes,
        payload_hash: str,
    ) -> WebhookInsertResult:
        return WebhookInsertResult(created=False, job_id=None)

    async def mark_processed(self, webhook_event_id: UUID) -> bool:
        return False


class NoopDispatcher:
    async def dispatch(self, job_id: UUID) -> None:
        return None


class NoopOutboxStore:
    async def pending_dispatch_ids(self, *, limit: int) -> list[UUID]:
        return []

    async def claim(self, *, job_id: UUID, worker_id: str) -> OutboxClaimResult:
        return OutboxClaimResult(OutboxClaimState.COMPLETED)

    async def complete(self, *, job_id: UUID, worker_id: str) -> bool:
        return False


def test_liveness_has_no_dependency_or_secret_details() -> None:
    app = create_application(
        webhook_secret="fixture-webhook-secret",
        webhook_store=NoopStore(),
        job_dispatcher=NoopDispatcher(),
        mount_inngest=False,
    )

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"service": "acsa-gateway", "status": "alive"}
    assert "fixture-webhook-secret" not in response.text


@pytest.mark.filterwarnings(
    "ignore:Support for class-based `config` is deprecated:DeprecationWarning"
)
def test_inngest_mount_registers_the_outbox_ready_function_at_the_default_path() -> None:
    client = inngest.Inngest(
        app_id="acsa-gateway-test",
        is_production=False,
    )
    outbox_store = NoopOutboxStore()
    webhook_store = NoopStore()
    app = create_application(
        webhook_secret="fixture-webhook-secret",
        webhook_store=webhook_store,
        job_dispatcher=NoopDispatcher(),
        mount_inngest=True,
        inngest_client=client,
        inngest_functions=[
            create_outbox_ready_function(client, outbox_store, webhook_store),
            create_outbox_sweep_function(client, outbox_store, NoopDispatcher()),
        ],
    )

    response = TestClient(app).get("/api/inngest")

    assert response.status_code == 200
    assert response.json()["function_count"] == 2
