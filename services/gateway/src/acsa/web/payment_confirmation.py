"""Safe Razorpay launch configuration and browser confirmation routes."""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from acsa.security.browser_sessions import BrowserAuthorization, require_browser
from acsa.services.payment_finalization import (
    EvidenceSource,
    FinalizationOutcome,
    PaymentLaunchConfiguration,
)


class PaymentConfirmationServicePort(Protocol):
    async def payment_launch_configuration(
        self, attempt_id: str
    ) -> PaymentLaunchConfiguration | None: ...

    async def finalize_payment(
        self,
        payment_attempt_id: str,
        evidence_source: EvidenceSource,
    ) -> FinalizationOutcome: ...


class PaymentConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1, max_length=64)
    razorpay_payment_id: str = Field(min_length=1, max_length=128)
    razorpay_signature: str = Field(min_length=64, max_length=64)


def create_payment_confirmation_router(
    service: PaymentConfirmationServicePort,
    authorization: BrowserAuthorization | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/payments/razorpay/launch/{attempt_id}")
    async def payment_launch(attempt_id: str, request: Request) -> JSONResponse:
        await require_browser(authorization, request, attempt_id=attempt_id)
        configuration = await service.payment_launch_configuration(attempt_id)
        if configuration is None:
            return _response(404, "not_found")
        return JSONResponse(
            {
                "checkout_id": configuration.checkout_id,
                "attempt_id": configuration.attempt_id,
                "key_id": configuration.provider_key_id,
                "order_id": configuration.provider_order_id,
                "amount": configuration.amount_minor,
                "currency": configuration.currency,
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/payments/razorpay/confirm")
    async def confirm_payment(body: PaymentConfirmationRequest, request: Request) -> JSONResponse:
        await require_browser(authorization, request, attempt_id=body.attempt_id)
        outcome = await service.finalize_payment(
            body.attempt_id,
            EvidenceSource.browser(
                payment_id=body.razorpay_payment_id,
                signature=body.razorpay_signature,
            ),
        )
        if outcome is FinalizationOutcome.REJECTED:
            return _response(401, outcome.value)
        if outcome is FinalizationOutcome.NOT_FOUND:
            return _response(404, outcome.value)
        if outcome in {
            FinalizationOutcome.RECONCILING,
            FinalizationOutcome.INVENTORY_EXCEPTION,
        }:
            return _response(202, outcome.value)
        return _response(200, outcome.value)

    return router


def _response(status_code: int, value: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": value},
        headers={"Cache-Control": "no-store"},
    )
