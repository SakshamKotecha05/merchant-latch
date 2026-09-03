from __future__ import annotations

import os

import inngest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from acsa.adapters.inngest.dispatcher import InngestJobDispatcher
from acsa.adapters.postgres.outbox import PostgresOutboxStore
from acsa.adapters.postgres.webhooks import PostgresWebhookStore
from acsa.application import create_application
from acsa.config import load_gateway_settings
from acsa.inngest_functions import create_outbox_ready_function, create_outbox_sweep_function


def create_runtime_application() -> FastAPI:
    settings = load_gateway_settings(os.environ)
    engine = create_async_engine(settings.database_url.unicode_string())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    outbox_store = PostgresOutboxStore(session_factory)
    webhook_store = PostgresWebhookStore(session_factory)
    inngest_client = inngest.Inngest(
        app_id="acsa-gateway",
        event_key=settings.inngest_event_key.get_secret_value(),
        signing_key=settings.inngest_signing_key.get_secret_value(),
    )

    return create_application(
        webhook_secret=settings.razorpay_webhook_secret.get_secret_value(),
        webhook_store=webhook_store,
        job_dispatcher=InngestJobDispatcher(inngest_client, outbox_store),
        mount_inngest=True,
        inngest_client=inngest_client,
        inngest_functions=[
            create_outbox_ready_function(inngest_client, outbox_store, webhook_store),
            create_outbox_sweep_function(
                inngest_client,
                outbox_store,
                InngestJobDispatcher(inngest_client, outbox_store),
            ),
        ],
    )


app: FastAPI = create_runtime_application()
