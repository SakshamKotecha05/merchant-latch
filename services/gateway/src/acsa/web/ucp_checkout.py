"""HTTP routes for the initial signed UCP checkout slice."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import orjson
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from acsa.domain.ucp_checkout import SHOPPING_SERVICE_PATH, UCP_VERSION, create_escalated_checkout
from acsa.ports.ucp_checkouts import CheckoutPersistenceOutcome, UCPCheckoutStorePort
from acsa.security.ucp_signatures import (
    UCPVerificationError,
    export_public_jwk,
    sign_response,
    verify_request,
)


def create_ucp_checkout_router(
    *,
    store: UCPCheckoutStorePort,
    buyer_public_key: ec.EllipticCurvePublicKey,
    buyer_key_id: str,
    merchant_private_key: ec.EllipticCurvePrivateKey,
    merchant_key_id: str,
    public_gateway_url: str,
    public_merchant_url: str,
) -> APIRouter:
    router = APIRouter()
    base_url = public_gateway_url.rstrip("/")
    merchant_url = public_merchant_url.rstrip("/")

    @router.get("/.well-known/ucp")
    async def discovery() -> JSONResponse:
        return JSONResponse(
            {
                "ucp": {
                    "version": UCP_VERSION,
                    "services": {
                        "dev.ucp.shopping": [
                            {
                                "version": UCP_VERSION,
                                "spec": f"https://ucp.dev/{UCP_VERSION}/specification/overview",
                                "transport": "rest",
                                "schema": f"https://ucp.dev/{UCP_VERSION}/services/shopping/rest.openapi.json",
                                "endpoint": f"{base_url}{SHOPPING_SERVICE_PATH}",
                            }
                        ]
                    },
                    "capabilities": {
                        "dev.ucp.shopping.checkout": [
                            {
                                "version": UCP_VERSION,
                                "spec": f"https://ucp.dev/{UCP_VERSION}/specification/checkout",
                                "schema": f"https://ucp.dev/{UCP_VERSION}/schemas/shopping/checkout.json",
                            }
                        ]
                    },
                    "payment_handlers": {},
                },
                "keys": [
                    export_public_jwk(merchant_private_key.public_key(), key_id=merchant_key_id)
                ],
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    @router.post(f"{SHOPPING_SERVICE_PATH}/checkout-sessions")
    async def create_checkout(request: Request) -> Response:
        raw_body = await request.body()
        verified = _verify(request, raw_body, base_url, buyer_public_key, buyer_key_id)
        if isinstance(verified, JSONResponse):
            return verified
        try:
            body = orjson.loads(raw_body)
            line_items = body["line_items"]
            if not isinstance(line_items, list) or not line_items:
                raise ValueError
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            return _error(400, "invalid_request", "The checkout request must include line_items.")
        idempotency_key = request.headers["Idempotency-Key"]
        checkout_id = f"chk_{uuid4().hex}"
        checkout = create_escalated_checkout(
            checkout_id=checkout_id,
            buyer_key_id=buyer_key_id,
            line_items=line_items,
            continue_url=f"{merchant_url}/checkout/{checkout_id}",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        result = await store.create_or_replay(
            buyer_key_id=buyer_key_id,
            nonce=verified.nonce,
            nonce_expires_at=verified.expires_at,
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            checkout=checkout,
        )
        if result.outcome in {
            CheckoutPersistenceOutcome.CONFLICT,
            CheckoutPersistenceOutcome.NONCE_REPLAY,
        }:
            return _error(
                409, "replay_conflict", "The checkout request conflicts with a prior request."
            )
        if result.checkout is None:
            return _error(500, "checkout_unavailable", "The checkout is unavailable.")
        return _signed_response(
            result.checkout.response_body,
            201 if result.outcome is CheckoutPersistenceOutcome.CREATED else 200,
            request,
            merchant_private_key,
            merchant_key_id,
        )

    @router.get(f"{SHOPPING_SERVICE_PATH}/checkout-sessions/{{checkout_id}}")
    async def get_checkout(checkout_id: str, request: Request) -> Response:
        verified = _verify(request, b"", base_url, buyer_public_key, buyer_key_id)
        if isinstance(verified, JSONResponse):
            return verified
        checkout = await store.get(checkout_id)
        if checkout is None:
            return _error(404, "checkout_not_found", "The checkout does not exist.")
        return _signed_response(
            checkout.response_body, 200, request, merchant_private_key, merchant_key_id
        )

    return router


def _verify(
    request: Request,
    raw_body: bytes,
    base_url: str,
    public_key: ec.EllipticCurvePublicKey,
    key_id: str,
) -> Any:
    headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
    signed_request = httpx.Request(
        request.method, f"{base_url}{request.url.path}", headers=headers, content=raw_body
    )
    try:
        return verify_request(signed_request, public_key=public_key, expected_key_id=key_id)
    except UCPVerificationError:
        return _error(401, "authentication_failed", "The UCP request signature is invalid.")


def _signed_response(
    body: bytes,
    status_code: int,
    request: Request,
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str,
) -> Response:
    signed = httpx.Response(
        status_code,
        headers={"Content-Type": "application/json"},
        content=body,
        request=httpx.Request(request.method, str(request.url)),
    )
    sign_response(
        signed,
        private_key=private_key,
        key_id=key_id,
        created=datetime.now(UTC),
        expires=datetime.now(UTC) + timedelta(minutes=5),
    )
    return Response(
        content=body,
        status_code=status_code,
        media_type="application/json",
        headers={
            "Content-Digest": signed.headers["Content-Digest"],
            "Signature-Input": signed.headers["Signature-Input"],
            "Signature": signed.headers["Signature"],
        },
    )


def _error(status_code: int, code: str, content: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "content": content})
