"""HTTP routes for the initial signed UCP checkout slice."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import orjson
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from acsa.domain.commerce import CommerceMutationOutcome, CommerceMutationResult, RequestedLine
from acsa.domain.ucp_checkout import SHOPPING_SERVICE_PATH, UCP_VERSION
from acsa.security.ucp_signatures import (
    UCPVerificationError,
    export_public_jwk,
    sign_response,
    verify_request,
)
from acsa.services.commerce import CommerceService


def create_ucp_checkout_router(
    *,
    commerce_service: CommerceService,
    buyer_public_key: ec.EllipticCurvePublicKey,
    buyer_key_id: str,
    merchant_private_key: ec.EllipticCurvePrivateKey,
    merchant_key_id: str,
    public_gateway_url: str,
) -> APIRouter:
    router = APIRouter()
    base_url = public_gateway_url.rstrip("/")

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

    @router.get("/checkout/{checkout_id}")
    async def merchant_handoff(checkout_id: str) -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Merchant review required | MerchantLatch</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    body { align-items: center; background: #0f172a; color: #f8fafc; display: flex; margin: 0;
      min-height: 100dvh; padding: 24px; }
    main { margin: auto; max-width: 40rem; width: 100%; }
    section { border: 1px solid #334155; border-radius: 16px; padding: clamp(24px, 6vw, 48px); }
    p { color: #cbd5e1; font-size: 1rem; line-height: 1.6; max-width: 38rem; }
    h1 { font-size: clamp(2rem, 8vw, 4rem); letter-spacing: -0.04em; line-height: 1;
      margin: 16px 0; }
    .label { color: #fbbf24; font-size: 0.875rem; font-weight: 700; letter-spacing: 0.08em;
      text-transform: uppercase; }
  </style>
</head>
<body>
  <main>
    <section aria-labelledby="review-heading">
      <p class="label">MerchantLatch</p>
      <h1 id="review-heading">Merchant review required</h1>
      <p>This checkout needs merchant approval before any payment action can continue.</p>
      <p>For your security, checkout and payment details are available only in the merchant’s
      authenticated workflow.</p>
    </section>
  </main>
</body>
</html>""",
            headers={"Cache-Control": "no-store"},
        )

    @router.post(f"{SHOPPING_SERVICE_PATH}/checkout-sessions")
    async def create_checkout(request: Request) -> Response:
        raw_body = await request.body()
        verified = _verify(request, raw_body, base_url, buyer_public_key, buyer_key_id)
        if isinstance(verified, JSONResponse):
            return verified
        try:
            body = orjson.loads(raw_body)
            requested_lines = _requested_lines(body)
            budget_minor = _budget_minor(body)
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            return _error(400, "invalid_request", "The checkout request must include line_items.")
        result = await commerce_service.create_checkout(
            buyer_key_id=buyer_key_id,
            nonce=verified.nonce,
            nonce_expires_at=verified.expires_at,
            idempotency_key=request.headers["Idempotency-Key"],
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            requested_lines=requested_lines,
            budget_minor=budget_minor,
        )
        return _mutation_response(
            result, request, merchant_private_key, merchant_key_id, created_status=201
        )

    @router.put(f"{SHOPPING_SERVICE_PATH}/checkout-sessions/{{checkout_id}}")
    async def update_checkout(checkout_id: str, request: Request) -> Response:
        raw_body = await request.body()
        verified = _verify(request, raw_body, base_url, buyer_public_key, buyer_key_id)
        if isinstance(verified, JSONResponse):
            return verified
        try:
            body = orjson.loads(raw_body)
            expected_version = _expected_version(body)
            requested_lines = _requested_lines(body)
            budget_minor = _budget_minor(body)
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            return _error(400, "invalid_request", "The checkout update is invalid.")
        result = await commerce_service.update_checkout(
            checkout_id=checkout_id,
            buyer_key_id=buyer_key_id,
            nonce=verified.nonce,
            nonce_expires_at=verified.expires_at,
            expected_version=expected_version,
            idempotency_key=request.headers["Idempotency-Key"],
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            requested_lines=requested_lines,
            budget_minor=budget_minor,
        )
        return _mutation_response(result, request, merchant_private_key, merchant_key_id)

    @router.delete(f"{SHOPPING_SERVICE_PATH}/checkout-sessions/{{checkout_id}}")
    async def cancel_checkout(checkout_id: str, request: Request) -> Response:
        raw_body = await request.body()
        verified = _verify(request, raw_body, base_url, buyer_public_key, buyer_key_id)
        if isinstance(verified, JSONResponse):
            return verified
        try:
            body = orjson.loads(raw_body)
            expected_version = _expected_version(body)
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            return _error(400, "invalid_request", "The checkout cancellation is invalid.")
        result = await commerce_service.cancel_checkout(
            checkout_id=checkout_id,
            buyer_key_id=buyer_key_id,
            nonce=verified.nonce,
            nonce_expires_at=verified.expires_at,
            expected_version=expected_version,
            idempotency_key=request.headers["Idempotency-Key"],
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
        )
        return _mutation_response(result, request, merchant_private_key, merchant_key_id)

    @router.get(f"{SHOPPING_SERVICE_PATH}/checkout-sessions/{{checkout_id}}")
    async def get_checkout(checkout_id: str, request: Request) -> Response:
        verified = _verify(request, b"", base_url, buyer_public_key, buyer_key_id)
        if isinstance(verified, JSONResponse):
            return verified
        checkout = await commerce_service.get_checkout(checkout_id, buyer_key_id=buyer_key_id)
        if checkout is None:
            return _error(404, "checkout_not_found", "The checkout does not exist.")
        return _signed_response(
            checkout.canonical_bytes, 200, request, merchant_private_key, merchant_key_id
        )

    return router


def _requested_lines(body: object) -> list[RequestedLine]:
    if not isinstance(body, dict):
        raise ValueError
    line_items = body["line_items"]
    if not isinstance(line_items, list) or not line_items:
        raise ValueError
    requested: list[RequestedLine] = []
    for line in line_items:
        if not isinstance(line, dict) or not isinstance(line.get("item"), dict):
            raise ValueError
        variant_id = line["item"].get("id")
        quantity = line.get("quantity")
        if not isinstance(variant_id, str) or not variant_id:
            raise ValueError
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError
        requested.append(RequestedLine(variant_id=variant_id, quantity=quantity))
    return requested


def _budget_minor(body: object) -> int | None:
    if not isinstance(body, dict):
        raise ValueError
    value = body.get("budget_minor")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return int(value)


def _expected_version(body: object) -> int:
    if not isinstance(body, dict):
        raise ValueError
    value = body.get("expected_version")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError
    return int(value)


def _mutation_response(
    result: CommerceMutationResult,
    request: Request,
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str,
    *,
    created_status: int = 200,
) -> Response:
    if result.outcome is CommerceMutationOutcome.CONFLICT:
        return _error(409, "replay_conflict", "The request conflicts with a prior request.")
    if result.outcome is CommerceMutationOutcome.STALE:
        return _error(409, "checkout_version_conflict", "The checkout version is stale.")
    if result.outcome is CommerceMutationOutcome.NOT_FOUND:
        return _error(404, "checkout_not_found", "The checkout does not exist.")
    if result.outcome is CommerceMutationOutcome.BLOCKED:
        rule_id = result.rule_ids[0] if result.rule_ids else "checkout_blocked"
        return _error(422, rule_id, "The merchant policy blocked this checkout request.")
    if result.response_body is None:
        return _error(500, "checkout_unavailable", "The checkout is unavailable.")
    status_code = created_status if result.outcome is CommerceMutationOutcome.CREATED else 200
    return _signed_response(result.response_body, status_code, request, private_key, key_id)


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
