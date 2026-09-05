from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

import inngest
import pytest
from inngest.experimental import mocked
from inngest.experimental.mocked.consts import Status

from acsa.adapters.postgres.outbox import ClaimedOutboxJob
from acsa.inngest_functions import (
    create_lease_expiry_function,
    create_outbox_ready_function,
    create_outbox_sweep_function,
)
from acsa.ports.jobs import OutboxClaimResult, OutboxClaimState, OutboxFailureOutcome
from acsa.ports.webhooks import WebhookFinalizationWork, WebhookRefundWork
from acsa.services.payment_finalization import EvidenceSource, FinalizationOutcome
from acsa.services.payment_orders import PaymentOrderOutcome
from acsa.services.refunds import RefundOutcome


@dataclass
class SweepOutboxStore:
    pending_job_ids: list[UUID]
    limits: list[int] = field(default_factory=list)

    async def pending_dispatch_ids(self, *, limit: int) -> list[UUID]:
        self.limits.append(limit)
        return self.pending_job_ids[:limit]


@dataclass
class SweepDispatcher:
    failed_job_id: UUID | None = None
    dispatched_job_ids: list[UUID] = field(default_factory=list)

    async def dispatch(self, job_id: UUID) -> None:
        self.dispatched_job_ids.append(job_id)
        if job_id == self.failed_job_id:
            raise RuntimeError("dispatch failed")


@dataclass
class WorkerOutboxStore:
    job: ClaimedOutboxJob | None
    failure_outcome: OutboxFailureOutcome | None = None
    claim_requests: list[tuple[UUID, str]] = field(default_factory=list)
    completion_requests: list[tuple[UUID, str]] = field(default_factory=list)
    reschedule_requests: list[tuple[UUID, str, int]] = field(default_factory=list)
    failure_requests: list[tuple[UUID, str, str, datetime | None]] = field(default_factory=list)

    async def claim(self, *, job_id: UUID, worker_id: str) -> OutboxClaimResult:
        self.claim_requests.append((job_id, worker_id))
        if self.job is None:
            return OutboxClaimResult(OutboxClaimState.COMPLETED)
        return OutboxClaimResult(OutboxClaimState.CLAIMED, self.job)

    async def complete(self, *, job_id: UUID, worker_id: str) -> bool:
        self.completion_requests.append((job_id, worker_id))
        return True

    async def reschedule(self, *, job_id: UUID, worker_id: str, delay_seconds: int) -> bool:
        self.reschedule_requests.append((job_id, worker_id, delay_seconds))
        return True

    async def fail(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_code: str,
        retry_at: datetime | None,
    ) -> OutboxFailureOutcome:
        self.failure_requests.append((job_id, worker_id, error_code, retry_at))
        if self.failure_outcome is not None:
            return self.failure_outcome
        if retry_at is None:
            return OutboxFailureOutcome.DEAD_LETTERED
        return OutboxFailureOutcome.RETRY_SCHEDULED


@dataclass
class WorkerWebhookStore:
    work: WebhookFinalizationWork | None = None
    refund_work: WebhookRefundWork | None = None
    processed_webhook_ids: list[UUID] = field(default_factory=list)

    async def load_finalization_work(
        self, webhook_event_id: UUID
    ) -> WebhookFinalizationWork | None:
        return self.work

    async def load_refund_work(self, webhook_event_id: UUID) -> WebhookRefundWork | None:
        return self.refund_work

    async def mark_processed(self, webhook_event_id: UUID) -> bool:
        self.processed_webhook_ids.append(webhook_event_id)
        return True


@dataclass
class LeaseHoldingOutboxStore:
    job: ClaimedOutboxJob
    claim_requests: list[tuple[UUID, str]] = field(default_factory=list)
    completion_requests: list[tuple[UUID, str]] = field(default_factory=list)
    failure_requests: list[tuple[UUID, str, str, datetime | None]] = field(default_factory=list)

    async def claim(self, *, job_id: UUID, worker_id: str) -> OutboxClaimResult:
        self.claim_requests.append((job_id, worker_id))
        if len(self.claim_requests) == 1:
            return OutboxClaimResult(OutboxClaimState.CLAIMED, self.job)
        return OutboxClaimResult(OutboxClaimState.LEASED)

    async def complete(self, *, job_id: UUID, worker_id: str) -> bool:
        self.completion_requests.append((job_id, worker_id))
        return True

    async def reschedule(self, *, job_id: UUID, worker_id: str, delay_seconds: int) -> bool:
        raise AssertionError("failing webhook jobs must not be rescheduled")

    async def fail(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error_code: str,
        retry_at: datetime | None,
    ) -> OutboxFailureOutcome:
        self.failure_requests.append((job_id, worker_id, error_code, retry_at))
        return OutboxFailureOutcome.RETRY_SCHEDULED


class FailingWorkerWebhookStore:
    async def load_finalization_work(
        self, webhook_event_id: UUID
    ) -> WebhookFinalizationWork | None:
        return None

    async def load_refund_work(self, webhook_event_id: UUID) -> WebhookRefundWork | None:
        return None

    async def mark_processed(self, webhook_event_id: UUID) -> bool:
        raise RuntimeError("webhook handling failed")


@dataclass
class PaymentOrderServiceStub:
    outcome: PaymentOrderOutcome
    attempt_ids: list[str] = field(default_factory=list)

    async def process(self, attempt_id: str) -> PaymentOrderOutcome:
        self.attempt_ids.append(attempt_id)
        return self.outcome


@dataclass
class PaymentFinalizationServiceStub:
    outcome: FinalizationOutcome = FinalizationOutcome.COMPLETED
    requests: list[tuple[str, EvidenceSource]] = field(default_factory=list)

    async def finalize_payment(
        self, attempt_id: str, source: EvidenceSource
    ) -> FinalizationOutcome:
        self.requests.append((attempt_id, source))
        return self.outcome


@dataclass
class RefundServiceStub:
    outcome: RefundOutcome = RefundOutcome.REFUNDED
    released: int = 0
    attempts: list[str] = field(default_factory=list)
    webhook_attempts: list[tuple[str, str]] = field(default_factory=list)
    limits: list[int] = field(default_factory=list)

    async def process(self, attempt_id: str) -> RefundOutcome:
        self.attempts.append(attempt_id)
        return self.outcome

    async def reconcile_webhook(self, attempt_id: str, provider_refund_id: str) -> RefundOutcome:
        self.webhook_attempts.append((attempt_id, provider_refund_id))
        return self.outcome

    async def release_expired_leases(self, *, limit: int) -> int:
        self.limits.append(limit)
        return self.released


@pytest.fixture(autouse=True)
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(None)


def test_outbox_ready_function_claims_handles_and_completes_a_webhook_job() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = UUID("9b054ab0-226c-4da1-b5b6-cffb5b3571f8")
    webhook_event_id = UUID("c47f7b80-0aaf-43a6-94d8-38e19ac96d80")
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="process_razorpay_webhook",
            aggregate_type="webhook_event",
            aggregate_id=str(webhook_event_id),
            payload={"webhook_event_id": str(webhook_event_id)},
            attempt_count=1,
        )
    )
    webhook_store = WorkerWebhookStore()
    function = create_outbox_ready_function(client, outbox_store, webhook_store)

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "completed"}
    assert outbox_store.claim_requests == [(job_id, "test")]
    assert webhook_store.processed_webhook_ids == [webhook_event_id]
    assert outbox_store.completion_requests == [(job_id, "test")]


def test_outbox_ready_function_routes_webhook_evidence_through_finalization() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    webhook_event_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="process_razorpay_webhook",
            aggregate_type="webhook_event",
            aggregate_id=str(webhook_event_id),
            payload={"webhook_event_id": str(webhook_event_id)},
            attempt_count=1,
        )
    )
    webhook_store = WorkerWebhookStore(
        work=WebhookFinalizationWork(
            attempt_id="att_1",
            payment_id="pay_1",
            order_id="order_1",
        )
    )
    finalizer = PaymentFinalizationServiceStub()
    function = create_outbox_ready_function(
        client,
        outbox_store,
        webhook_store,
        payment_finalization_service=finalizer,
    )

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert finalizer.requests == [
        (
            "att_1",
            EvidenceSource.webhook(
                payment_id="pay_1",
                order_id="order_1",
                webhook_event_id=str(webhook_event_id),
            ),
        )
    ]


def test_outbox_ready_function_reconciles_a_verified_refund_webhook() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    webhook_event_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="process_razorpay_webhook",
            aggregate_type="webhook_event",
            aggregate_id=str(webhook_event_id),
            payload={"webhook_event_id": str(webhook_event_id)},
            attempt_count=1,
        )
    )
    webhook_store = WorkerWebhookStore(
        refund_work=WebhookRefundWork(attempt_id="att_1", refund_id="rfnd_1")
    )
    refund_service = RefundServiceStub()
    function = create_outbox_ready_function(
        client,
        outbox_store,
        webhook_store,
        refund_service=refund_service,
    )

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert refund_service.webhook_attempts == [("att_1", "rfnd_1")]
    assert webhook_store.processed_webhook_ids == [webhook_event_id]
    assert outbox_store.completion_requests == [(job_id, "test")]


def test_outbox_ready_function_reschedules_a_pending_refund_webhook() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    webhook_event_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="process_razorpay_webhook",
            aggregate_type="webhook_event",
            aggregate_id=str(webhook_event_id),
            payload={"webhook_event_id": str(webhook_event_id)},
            attempt_count=1,
        )
    )
    webhook_store = WorkerWebhookStore(
        refund_work=WebhookRefundWork(attempt_id="att_1", refund_id="rfnd_1")
    )
    function = create_outbox_ready_function(
        client,
        outbox_store,
        webhook_store,
        refund_service=RefundServiceStub(outcome=RefundOutcome.PENDING),
    )

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "retry_scheduled"}
    assert webhook_store.processed_webhook_ids == []
    assert outbox_store.reschedule_requests == [(job_id, "test", 5)]


def test_outbox_ready_function_rejects_missing_job_id() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    function = create_outbox_ready_function(client, WorkerOutboxStore(None), WorkerWebhookStore())

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={}),
        client,
    )

    assert result.status is Status.FAILED
    assert isinstance(result.error, ValueError)


def test_outbox_ready_function_rejects_a_malformed_job_id() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    function = create_outbox_ready_function(client, WorkerOutboxStore(None), WorkerWebhookStore())

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": "not-a-uuid"}),
        client,
    )

    assert result.status is Status.FAILED
    assert isinstance(result.error, ValueError)


def test_outbox_ready_function_ignores_an_already_completed_job() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(None)
    webhook_store = WorkerWebhookStore()
    function = create_outbox_ready_function(client, outbox_store, webhook_store)

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "ignored"}
    assert webhook_store.processed_webhook_ids == []
    assert outbox_store.completion_requests == []


def test_outbox_ready_function_persists_retryable_handler_failure() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    webhook_event_id = uuid4()
    outbox_store = LeaseHoldingOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="process_razorpay_webhook",
            aggregate_type="webhook_event",
            aggregate_id=str(webhook_event_id),
            payload={"webhook_event_id": str(webhook_event_id)},
            attempt_count=1,
        )
    )
    function = create_outbox_ready_function(client, outbox_store, FailingWorkerWebhookStore())

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "retry_scheduled"}
    assert len(outbox_store.failure_requests) == 1
    failure = outbox_store.failure_requests[0]
    assert failure[:3] == (job_id, "test", "job_processing_failed")
    assert failure[3] is not None
    assert outbox_store.completion_requests == []


def test_outbox_ready_function_dead_letters_a_permanently_invalid_job() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="unknown_job",
            aggregate_type="webhook_event",
            aggregate_id="unrelated",
            payload={},
            attempt_count=1,
        )
    )
    function = create_outbox_ready_function(client, outbox_store, WorkerWebhookStore())

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "dead_lettered"}
    assert outbox_store.failure_requests == [(job_id, "test", "invalid_job", None)]
    assert outbox_store.completion_requests == []


def test_outbox_ready_function_dead_letters_unknown_jobs_without_completing_them() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="unknown_job",
            aggregate_type="webhook_event",
            aggregate_id="unrelated",
            payload={},
            attempt_count=1,
        )
    )
    webhook_store = WorkerWebhookStore()
    function = create_outbox_ready_function(client, outbox_store, webhook_store)

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "dead_lettered"}
    assert outbox_store.failure_requests == [(job_id, "test", "invalid_job", None)]
    assert webhook_store.processed_webhook_ids == []
    assert outbox_store.completion_requests == []


def test_outbox_ready_function_dead_letters_malformed_webhook_payload() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="process_razorpay_webhook",
            aggregate_type="webhook_event",
            aggregate_id="unrelated",
            payload={},
            attempt_count=1,
        )
    )
    webhook_store = WorkerWebhookStore()
    function = create_outbox_ready_function(client, outbox_store, webhook_store)

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "dead_lettered"}
    assert outbox_store.failure_requests == [(job_id, "test", "invalid_job", None)]
    assert webhook_store.processed_webhook_ids == []
    assert outbox_store.completion_requests == []


def test_outbox_ready_function_completes_a_created_provider_order_job() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="create_provider_order",
            aggregate_type="payment_attempt",
            aggregate_id="att_1",
            payload={"attempt_id": "att_1"},
            attempt_count=1,
        )
    )
    service = PaymentOrderServiceStub(PaymentOrderOutcome.CREATED)
    function = create_outbox_ready_function(
        client,
        outbox_store,
        WorkerWebhookStore(),
        payment_order_service=service,
    )

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "completed"}
    assert service.attempt_ids == ["att_1"]
    assert outbox_store.completion_requests == [(job_id, "test")]


@pytest.mark.parametrize(
    ("attempt_count", "delay_seconds"),
    [(1, 2), (2, 5), (3, 10), (4, 20), (5, 40)],
)
def test_outbox_ready_function_reschedules_uncertain_order_without_recreating(
    attempt_count: int,
    delay_seconds: int,
) -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="create_provider_order",
            aggregate_type="payment_attempt",
            aggregate_id="att_1",
            payload={"attempt_id": "att_1"},
            attempt_count=attempt_count,
        )
    )
    service = PaymentOrderServiceStub(PaymentOrderOutcome.RECONCILING)
    function = create_outbox_ready_function(
        client,
        outbox_store,
        WorkerWebhookStore(),
        payment_order_service=service,
    )

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "retry_scheduled"}
    assert outbox_store.reschedule_requests == [(job_id, "test", delay_seconds)]
    assert outbox_store.completion_requests == []


def test_outbox_ready_function_dead_letters_exhausted_reconciliation() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="create_provider_order",
            aggregate_type="payment_attempt",
            aggregate_id="att_1",
            payload={"attempt_id": "att_1"},
            attempt_count=6,
        ),
        failure_outcome=OutboxFailureOutcome.DEAD_LETTERED,
    )
    service = PaymentOrderServiceStub(PaymentOrderOutcome.RECONCILING)
    function = create_outbox_ready_function(
        client,
        outbox_store,
        WorkerWebhookStore(),
        payment_order_service=service,
    )

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "dead_lettered"}
    assert len(outbox_store.failure_requests) == 1
    assert outbox_store.reschedule_requests == []
    assert outbox_store.completion_requests == []


def test_outbox_ready_function_completes_a_processed_refund_job() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="refund_captured_payment",
            aggregate_type="payment_attempt",
            aggregate_id="att_1",
            payload={"attempt_id": "att_1"},
            attempt_count=1,
        )
    )
    refund_service = RefundServiceStub()
    function = create_outbox_ready_function(
        client,
        outbox_store,
        WorkerWebhookStore(),
        refund_service=refund_service,
    )

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert refund_service.attempts == ["att_1"]
    assert outbox_store.completion_requests == [(job_id, "test")]


@pytest.mark.parametrize("outcome", [RefundOutcome.RETRY, RefundOutcome.PENDING])
def test_outbox_ready_function_reschedules_an_unconfirmed_refund(
    outcome: RefundOutcome,
) -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_id = uuid4()
    outbox_store = WorkerOutboxStore(
        ClaimedOutboxJob(
            id=job_id,
            job_type="refund_captured_payment",
            aggregate_type="payment_attempt",
            aggregate_id="att_1",
            payload={"attempt_id": "att_1"},
            attempt_count=1,
        )
    )
    function = create_outbox_ready_function(
        client,
        outbox_store,
        WorkerWebhookStore(),
        refund_service=RefundServiceStub(outcome=outcome),
    )

    result = mocked.trigger(
        function,
        inngest.Event(name="acsa/outbox.ready", data={"job_id": str(job_id)}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"status": "retry_scheduled"}
    assert outbox_store.reschedule_requests == [(job_id, "test", 5)]
    assert outbox_store.completion_requests == []


def test_lease_expiry_function_runs_a_bounded_sweep() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    refund_service = RefundServiceStub(released=3)
    function = create_lease_expiry_function(client, refund_service)

    result = mocked.trigger(
        function,
        inngest.Event(name="inngest/scheduled.timer", data={}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert result.output == {"released": 3}
    assert refund_service.limits == [100]


def test_outbox_sweep_reads_at_most_one_hundred_available_jobs() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    job_ids = [uuid4() for _ in range(101)]
    outbox_store = SweepOutboxStore(job_ids)
    dispatcher = SweepDispatcher()
    function = create_outbox_sweep_function(client, outbox_store, dispatcher)

    result = mocked.trigger(
        function,
        inngest.Event(name="inngest/scheduled.timer", data={}),
        client,
    )

    assert function._opts.local_id == "outbox-sweep"
    assert isinstance(function._triggers[0], inngest.TriggerCron)
    assert function._triggers[0].cron == "* * * * *"
    assert result.status is Status.COMPLETED
    assert outbox_store.limits == [100]
    assert dispatcher.dispatched_job_ids == job_ids[:100]


def test_outbox_sweep_continues_after_a_dispatch_failure() -> None:
    client = mocked.Inngest(app_id="acsa-gateway-test")
    first_job_id, failing_job_id, last_job_id = uuid4(), uuid4(), uuid4()
    outbox_store = SweepOutboxStore([first_job_id, failing_job_id, last_job_id])
    dispatcher = SweepDispatcher(failed_job_id=failing_job_id)
    function = create_outbox_sweep_function(client, outbox_store, dispatcher)

    result = mocked.trigger(
        function,
        inngest.Event(name="inngest/scheduled.timer", data={}),
        client,
    )

    assert result.status is Status.COMPLETED
    assert dispatcher.dispatched_job_ids == [first_job_id, failing_job_id, last_job_id]
