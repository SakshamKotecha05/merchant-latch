from __future__ import annotations

import os

import inngest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from acsa.adapters.inngest.dispatcher import InngestJobDispatcher
from acsa.adapters.postgres.webhooks import PostgresWebhookStore
from acsa.application import create_application
from acsa.config import load_gateway_settings
from acsa.inngest_functions import create_outbox_ready_function


def create_runtime_application() -> FastAPI:
    settings = load_gateway_settings(os.environ)
    engine = create_async_engine(settings.database_url.unicode_string())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    inngest_client = inngest.Inngest(
        app_id="acsa-gateway",
        event_key=settings.inngest_event_key.get_secret_value(),
        signing_key=settings.inngest_signing_key.get_secret_value(),
    )

    return create_application(
        webhook_secret=settings.razorpay_webhook_secret.get_secret_value(),
        webhook_store=PostgresWebhookStore(session_factory),
        job_dispatcher=InngestJobDispatcher(inngest_client),
        mount_inngest=True,
        inngest_client=inngest_client,
        inngest_functions=[create_outbox_ready_function(inngest_client)],
    )


app: FastAPI = create_runtime_application()
