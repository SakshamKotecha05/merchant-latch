from __future__ import annotations

from typing import Any

import inngest
from fastapi import FastAPI

from acsa.ports.jobs import JobDispatcherPort
from acsa.ports.webhooks import WebhookStorePort
from acsa.web.razorpay_webhooks import create_razorpay_webhook_router


def create_application(
    *,
    webhook_secret: str,
    webhook_store: WebhookStorePort,
    job_dispatcher: JobDispatcherPort,
    mount_inngest: bool = False,
    inngest_client: inngest.Inngest | None = None,
    inngest_functions: list[inngest.Function[Any]] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Agentic Checkout Safety Adapter Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"service": "acsa-gateway", "status": "alive"}

    app.include_router(
        create_razorpay_webhook_router(
            webhook_secret=webhook_secret,
            store=webhook_store,
            dispatcher=job_dispatcher,
        )
    )

    if mount_inngest:
        if inngest_client is None or inngest_functions is None:
            raise ValueError("Inngest client and functions are required when mounting Inngest")
        from inngest import fast_api

        fast_api.serve(app, inngest_client, inngest_functions)

    return app
