from __future__ import annotations

import os

import httpx
import inngest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from acsa.adapters.inngest.dispatcher import InngestJobDispatcher
from acsa.adapters.postgres.commerce import PostgresCommerceStore
from acsa.adapters.postgres.outbox import PostgresOutboxStore
from acsa.adapters.postgres.payment_orders import PostgresPaymentOrderStore
from acsa.adapters.postgres.webhooks import PostgresWebhookStore
from acsa.adapters.razorpay.client import RazorpayClient
from acsa.application import create_application
from acsa.config import ConfigurationError, load_gateway_settings
from acsa.inngest_functions import create_outbox_ready_function, create_outbox_sweep_function
from acsa.security.continue_tokens import issue_continue_token
from acsa.security.ucp_signatures import import_public_jwk
from acsa.services.commerce import CommerceService
from acsa.services.payment_orders import PaymentOrderService
from acsa.web.catalog import create_catalog_router
from acsa.web.merchant_checkout import create_merchant_checkout_router
from acsa.web.ucp_checkout import create_ucp_checkout_router


def create_runtime_application() -> FastAPI:
    settings = load_gateway_settings(os.environ)
    engine = create_async_engine(settings.database_url.unicode_string())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    commerce_store = PostgresCommerceStore(session_factory)
    outbox_store = PostgresOutboxStore(session_factory)
    webhook_store = PostgresWebhookStore(session_factory)
    provider_http_client = httpx.AsyncClient(timeout=10)
    payment_order_service = PaymentOrderService(
        store=PostgresPaymentOrderStore(session_factory),
        provider=RazorpayClient(
            key_id=settings.razorpay_key_id,
            key_secret=settings.razorpay_key_secret.get_secret_value(),
            http_client=provider_http_client,
        ),
    )
    inngest_client = inngest.Inngest(
        app_id="acsa-gateway",
        event_key=settings.inngest_event_key.get_secret_value(),
        signing_key=settings.inngest_signing_key.get_secret_value(),
    )

    app = create_application(
        webhook_secret=settings.razorpay_webhook_secret.get_secret_value(),
        webhook_store=webhook_store,
        job_dispatcher=InngestJobDispatcher(inngest_client, outbox_store),
        mount_inngest=True,
        inngest_client=inngest_client,
        inngest_functions=[
            create_outbox_ready_function(
                inngest_client,
                outbox_store,
                webhook_store,
                payment_order_service=payment_order_service,
            ),
            create_outbox_sweep_function(
                inngest_client,
                outbox_store,
                InngestJobDispatcher(inngest_client, outbox_store),
            ),
        ],
    )
    app.router.add_event_handler("shutdown", provider_http_client.aclose)
    merchant_private_key = load_pem_private_key(
        settings.ucp_merchant_private_key.get_secret_value().encode("utf-8"),
        password=None,
    )
    if not isinstance(merchant_private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        merchant_private_key.curve, ec.SECP256R1
    ):
        raise ConfigurationError("UCP_MERCHANT_PRIVATE_KEY must contain a P-256 private key")
    app.include_router(create_catalog_router(commerce_store))
    commerce_service = CommerceService(
        store=commerce_store,
        merchant_id="merchant_demo",
        pickup_location_id="pickup_blr_01",
        public_merchant_url=str(settings.public_merchant_url),
        continue_token_issuer=lambda checkout_id, version, now: issue_continue_token(
            merchant_private_key,
            checkout_id=checkout_id,
            checkout_version=version,
            now=now,
        ),
    )
    app.include_router(
        create_ucp_checkout_router(
            commerce_service=commerce_service,
            buyer_public_key=import_public_jwk(settings.ucp_buyer_public_jwk),
            buyer_key_id=settings.ucp_buyer_key_id,
            merchant_private_key=merchant_private_key,
            merchant_key_id=settings.ucp_merchant_key_id,
            public_gateway_url=str(settings.public_gateway_url),
        )
    )
    app.include_router(
        create_merchant_checkout_router(
            commerce_service=commerce_service,
            merchant_public_key=merchant_private_key.public_key(),
        )
    )
    return app


app: FastAPI = create_runtime_application()
