from __future__ import annotations

from typing import Protocol
from uuid import UUID

import inngest

from acsa.ports.jobs import (
    JobDispatcherPort,
    OutboxClaimState,
    OutboxSweepStorePort,
    OutboxWorkerStorePort,
)
from acsa.ports.webhooks import WebhookProcessingStorePort
from acsa.services.payment_orders import PaymentOrderOutcome

_ORDER_RETRY_DELAYS = (2, 5, 10, 20, 40)


class PaymentOrderServicePort(Protocol):
    async def process(self, attempt_id: str) -> PaymentOrderOutcome: ...


def create_outbox_ready_function(
    client: inngest.Inngest,
    outbox_store: OutboxWorkerStorePort,
    webhook_store: WebhookProcessingStorePort,
    payment_order_service: PaymentOrderServicePort | None = None,
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
        if job.job_type == "create_provider_order":
            if payment_order_service is None:
                raise ValueError("Payment Order service is unavailable")
            attempt_id = job.payload.get("attempt_id")
            if not isinstance(attempt_id, str) or attempt_id != job.aggregate_id:
                raise ValueError("Invalid provider Order job payload")
            outcome = await payment_order_service.process(attempt_id)
            if outcome is PaymentOrderOutcome.RECONCILING:
                retry_index = job.attempt_count - 1
                if retry_index >= len(_ORDER_RETRY_DELAYS):
                    raise RuntimeError("Provider Order reconciliation exhausted")
                if not await outbox_store.reschedule(
                    job_id=job.id,
                    worker_id=context.run_id,
                    delay_seconds=_ORDER_RETRY_DELAYS[retry_index],
                ):
                    raise RuntimeError("Unable to reschedule outbox job")
                return {"status": "retry_scheduled"}
            if outcome is PaymentOrderOutcome.NOT_FOUND:
                raise ValueError("Referenced payment attempt does not exist")
            if not await outbox_store.complete(job_id=job.id, worker_id=context.run_id):
                raise RuntimeError("Unable to complete outbox job")
            return {"status": "completed"}
        if job.job_type != "process_razorpay_webhook":
            raise ValueError("Unsupported outbox job type")

        raw_webhook_event_id = job.payload.get("webhook_event_id")
        if not isinstance(raw_webhook_event_id, str):
            raise ValueError("Invalid webhook job payload")
        try:
            webhook_event_id = UUID(raw_webhook_event_id)
        except ValueError:
            raise ValueError("Invalid webhook job payload") from None

        if not await webhook_store.mark_processed(webhook_event_id):
            raise ValueError("Referenced webhook event does not exist")
        if not await outbox_store.complete(job_id=job.id, worker_id=context.run_id):
            raise RuntimeError("Unable to complete outbox job")
        return {"status": "completed"}

    return process_outbox_ready


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
