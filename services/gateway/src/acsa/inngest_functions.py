from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import inngest

from acsa.ports.jobs import (
    ClaimedJobPort,
    JobDispatcherPort,
    OutboxClaimState,
    OutboxFailureOutcome,
    OutboxSweepStorePort,
    OutboxWorkerStorePort,
)
from acsa.ports.webhooks import WebhookProcessingStorePort
from acsa.services.payment_finalization import EvidenceSource, FinalizationOutcome
from acsa.services.payment_orders import PaymentOrderOutcome
from acsa.services.refunds import RefundOutcome

_ORDER_RETRY_DELAYS = (2, 5, 10, 20, 40)
_REFUND_RETRY_DELAYS = (5, 15, 30, 60, 120, 300, 600)
_FAILURE_RETRY_DELAYS = (5, 15, 30, 60, 120, 300, 600)


class PaymentOrderServicePort(Protocol):
    async def process(self, attempt_id: str) -> PaymentOrderOutcome: ...


class PaymentFinalizationServicePort(Protocol):
    async def finalize_payment(
        self,
        payment_attempt_id: str,
        evidence_source: EvidenceSource,
    ) -> FinalizationOutcome: ...


class RefundServicePort(Protocol):
    async def process(self, attempt_id: str) -> RefundOutcome: ...

    async def reconcile_webhook(
        self, attempt_id: str, provider_refund_id: str
    ) -> RefundOutcome: ...

    async def release_expired_leases(self, *, limit: int) -> int: ...


def create_outbox_ready_function(
    client: inngest.Inngest,
    outbox_store: OutboxWorkerStorePort,
    webhook_store: WebhookProcessingStorePort,
    payment_order_service: PaymentOrderServicePort | None = None,
    payment_finalization_service: PaymentFinalizationServicePort | None = None,
    refund_service: RefundServicePort | None = None,
) -> inngest.Function[dict[str, str]]:
    @client.create_function(
        fn_id="outbox-ready",
        trigger=inngest.TriggerEvent(event="acsa/outbox.ready"),
    )
    async def process_outbox_ready(context: inngest.Context) -> dict[str, str]:
        raw_job_id = context.event.data.get("job_id")
        if not isinstance(raw_job_id, str):
            raise ValueError("event.data.job_id must be a UUID")

        try:
            job_id = UUID(raw_job_id)
        except ValueError:
            raise ValueError("event.data.job_id must be a UUID") from None

        claim = await outbox_store.claim(job_id=job_id, worker_id=context.run_id)
        if claim.state is OutboxClaimState.COMPLETED:
            return {"status": "ignored"}
        if claim.state is not OutboxClaimState.CLAIMED or claim.job is None:
            raise RuntimeError("Outbox job is not available")
        job = claim.job
        try:
            return await _process_claimed_job(
                job=job,
                worker_id=context.run_id,
                outbox_store=outbox_store,
                webhook_store=webhook_store,
                payment_order_service=payment_order_service,
                payment_finalization_service=payment_finalization_service,
                refund_service=refund_service,
            )
        except ValueError:
            failure_outcome = await outbox_store.fail(
                job_id=job.id,
                worker_id=context.run_id,
                error_code="invalid_job",
                retry_at=None,
            )
        except Exception:
            retry_index = job.attempt_count - 1
            retry_at = (
                datetime.now(UTC) + timedelta(seconds=_FAILURE_RETRY_DELAYS[retry_index])
                if retry_index < len(_FAILURE_RETRY_DELAYS)
                else None
            )
            failure_outcome = await outbox_store.fail(
                job_id=job.id,
                worker_id=context.run_id,
                error_code="job_processing_failed",
                retry_at=retry_at,
            )
        if failure_outcome is OutboxFailureOutcome.REJECTED:
            raise RuntimeError("Unable to persist outbox failure")
        return {"status": failure_outcome.value}

    return process_outbox_ready


async def _process_claimed_job(
    *,
    job: ClaimedJobPort,
    worker_id: str,
    outbox_store: OutboxWorkerStorePort,
    webhook_store: WebhookProcessingStorePort,
    payment_order_service: PaymentOrderServicePort | None,
    payment_finalization_service: PaymentFinalizationServicePort | None,
    refund_service: RefundServicePort | None,
) -> dict[str, str]:
    if job.job_type == "create_provider_order":
        if payment_order_service is None:
            raise ValueError("Payment Order service is unavailable")
        attempt_id = job.payload.get("attempt_id")
        if not isinstance(attempt_id, str) or attempt_id != job.aggregate_id:
            raise ValueError("Invalid provider Order job payload")
        order_outcome = await payment_order_service.process(attempt_id)
        if order_outcome is PaymentOrderOutcome.RECONCILING:
            return await _reschedule_job(
                job,
                worker_id,
                outbox_store,
                _ORDER_RETRY_DELAYS,
                "Provider Order reconciliation exhausted",
            )
        if order_outcome is PaymentOrderOutcome.NOT_FOUND:
            raise ValueError("Referenced payment attempt does not exist")
        return await _complete_job(job, worker_id, outbox_store)
    if job.job_type == "refund_captured_payment":
        if refund_service is None:
            raise ValueError("Refund service is unavailable")
        attempt_id = job.payload.get("attempt_id")
        if not isinstance(attempt_id, str) or attempt_id != job.aggregate_id:
            raise ValueError("Invalid refund job payload")
        refund_outcome = await refund_service.process(attempt_id)
        if refund_outcome in {RefundOutcome.RETRY, RefundOutcome.PENDING}:
            return await _reschedule_job(
                job,
                worker_id,
                outbox_store,
                _REFUND_RETRY_DELAYS,
                "Refund reconciliation exhausted",
            )
        if refund_outcome is RefundOutcome.NOT_FOUND:
            raise ValueError("Referenced payment attempt does not exist")
        return await _complete_job(job, worker_id, outbox_store)
    if job.job_type != "process_razorpay_webhook":
        raise ValueError("Unsupported outbox job type")

    raw_webhook_event_id = job.payload.get("webhook_event_id")
    if not isinstance(raw_webhook_event_id, str):
        raise ValueError("Invalid webhook job payload")
    try:
        webhook_event_id = UUID(raw_webhook_event_id)
    except ValueError:
        raise ValueError("Invalid webhook job payload") from None
    finalization_work = (
        await webhook_store.load_finalization_work(webhook_event_id)
        if payment_finalization_service is not None
        else None
    )
    if finalization_work is not None and payment_finalization_service is not None:
        finalization_outcome = await payment_finalization_service.finalize_payment(
            finalization_work.attempt_id,
            EvidenceSource.webhook(
                payment_id=finalization_work.payment_id,
                order_id=finalization_work.order_id,
                webhook_event_id=str(webhook_event_id),
            ),
        )
        if finalization_outcome in {
            FinalizationOutcome.RECONCILING,
            FinalizationOutcome.REJECTED,
            FinalizationOutcome.NOT_FOUND,
        }:
            raise RuntimeError("Webhook payment finalization is not conclusive")
    refund_work = (
        await webhook_store.load_refund_work(webhook_event_id)
        if refund_service is not None
        else None
    )
    if refund_work is not None and refund_service is not None:
        refund_outcome = await refund_service.reconcile_webhook(
            refund_work.attempt_id,
            refund_work.refund_id,
        )
        if refund_outcome in {RefundOutcome.RETRY, RefundOutcome.PENDING}:
            return await _reschedule_job(
                job,
                worker_id,
                outbox_store,
                _REFUND_RETRY_DELAYS,
                "Refund webhook reconciliation exhausted",
            )
        if refund_outcome is RefundOutcome.NOT_FOUND:
            raise RuntimeError("Refund webhook attempt no longer exists")
    if not await webhook_store.mark_processed(webhook_event_id):
        raise ValueError("Referenced webhook event does not exist")
    return await _complete_job(job, worker_id, outbox_store)


async def _reschedule_job(
    job: ClaimedJobPort,
    worker_id: str,
    outbox_store: OutboxWorkerStorePort,
    delays: tuple[int, ...],
    exhausted_message: str,
) -> dict[str, str]:
    retry_index = job.attempt_count - 1
    if retry_index >= len(delays):
        raise RuntimeError(exhausted_message)
    if not await outbox_store.reschedule(
        job_id=job.id,
        worker_id=worker_id,
        delay_seconds=delays[retry_index],
    ):
        raise RuntimeError("Unable to reschedule outbox job")
    return {"status": "retry_scheduled"}


async def _complete_job(
    job: ClaimedJobPort,
    worker_id: str,
    outbox_store: OutboxWorkerStorePort,
) -> dict[str, str]:
    if not await outbox_store.complete(job_id=job.id, worker_id=worker_id):
        raise RuntimeError("Unable to complete outbox job")
    return {"status": "completed"}


def create_outbox_sweep_function(
    client: inngest.Inngest,
    outbox_store: OutboxSweepStorePort,
    dispatcher: JobDispatcherPort,
) -> inngest.Function[dict[str, str]]:
    @client.create_function(
        fn_id="outbox-sweep",
        trigger=inngest.TriggerCron(cron="* * * * *"),
    )
    async def sweep_outbox(context: inngest.Context) -> dict[str, str]:
        job_ids = await outbox_store.pending_dispatch_ids(limit=100)
        for job_id in job_ids:
            try:
                await dispatcher.dispatch(job_id)
            except Exception:  # noqa: S112 - each job failure must not stop the bounded sweep.
                continue
        return {"status": "swept"}

    return sweep_outbox


def create_lease_expiry_function(
    client: inngest.Inngest,
    refund_service: RefundServicePort,
) -> inngest.Function[dict[str, int]]:
    @client.create_function(
        fn_id="lease-expiry",
        trigger=inngest.TriggerCron(cron="* * * * *"),
    )
    async def expire_leases(context: inngest.Context) -> dict[str, int]:
        del context
        return {"released": await refund_service.release_expired_leases(limit=100)}

    return expire_leases
