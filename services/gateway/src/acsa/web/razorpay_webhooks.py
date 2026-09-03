from __future__ import annotations

import asyncio
import hashlib
import logging

import orjson
from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from acsa.ports.jobs import JobDispatcherPort
from acsa.ports.webhooks import WebhookStorePort
from acsa.security.razorpay_signatures import verify_webhook_signature

LOGGER = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1_048_576


def create_razorpay_webhook_router(
    *,
    webhook_secret: str,
    store: WebhookStorePort,
    dispatcher: JobDispatcherPort,
    dispatch_timeout_seconds: float = 0.75,
) -> APIRouter:
    router = APIRouter()

    @router.post("/webhooks/razorpay", status_code=status.HTTP_204_NO_CONTENT)
    async def receive_razorpay_webhook(
        request: Request,
        x_razorpay_signature: str | None = Header(default=None),
        x_razorpay_event_id: str | None = Header(default=None),
    ) -> Response:
        raw_body = await request.body()
        if len(raw_body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        if not x_razorpay_signature or not verify_webhook_signature(
            raw_body,
            x_razorpay_signature,
            webhook_secret,
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        if not x_razorpay_event_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing x-razorpay-event-id",
            )

        try:
            payload = orjson.loads(raw_body)
            event_name = payload["event"]
            if not isinstance(event_name, str) or not event_name:
                raise ValueError
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid webhook payload",
            ) from None

        result = await store.insert_verified_event(
            event_id=x_razorpay_event_id,
            event_name=event_name,
            raw_payload=raw_body,
            payload_hash=hashlib.sha256(raw_body).hexdigest(),
        )
        if result.created and result.job_id is not None:
            try:
                await asyncio.wait_for(
                    dispatcher.dispatch(result.job_id),
                    timeout=dispatch_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning(
                    "Webhook job dispatch failed after durable commit",
                    extra={"job_id": str(result.job_id)},
                )

        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
