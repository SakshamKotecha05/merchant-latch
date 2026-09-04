"""HTTP routes for the initial signed UCP checkout slice."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
import orjson
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from acsa.adapters.postgres.ucp_protocol import (
    NewUCPExchange,
    TrustPin,
    UCPTrustError,
)
from acsa.domain.commerce import CommerceMutationOutcome, CommerceMutationResult, RequestedLine
from acsa.domain.ucp_checkout import SHOPPING_SERVICE_PATH, UCP_VERSION
from acsa.security.ucp_signatures import (
    UCPVerificationError,
    export_public_jwk,
    parse_signature_key_id,
    sign_response,
    verify_request,
)
from acsa.services.commerce import CommerceService
from acsa.ucp_profiles import BuyerIdentity, BuyerProfileError


class BuyerResolver(Protocol):
    async def resolve(self, ucp_agent: str, key_id: str) -> BuyerIdentity: ...


class UCPProtocolStore(Protocol):
    async def get_pin(self, origin: str) -> TrustPin | None: ...

    async def verify_or_pin(self, identity: BuyerIdentity, now: datetime) -> TrustPin: ...

    async def append_exchange(self, event: NewUCPExchange) -> object: ...


@dataclass(frozen=True, slots=True)
class AuthenticatedBuyer:
    identity: BuyerIdentity
    nonce: str
    nonce_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticationFailure:
    response: JSONResponse
    outcome: str
    identity: BuyerIdentity | None = None


class CheckoutTermsRejected(ValueError):
    def __init__(self, code: str, content: str) -> None:
        self.code = code
        self.content = content
        super().__init__(code)


def create_ucp_checkout_router(
    *,
    commerce_service: CommerceService,
    buyer_profile_resolver: BuyerResolver,
    protocol_store: UCPProtocolStore,
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
                "signing_keys": [
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
        started_at = datetime.now(UTC)
        raw_body = await request.body()
        response: Response
        buyer = await _authenticate(
            request, raw_body, base_url, buyer_profile_resolver, protocol_store
        )
        if isinstance(buyer, AuthenticationFailure):
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                buyer.response,
                started_at,
                buyer.outcome,
                identity=buyer.identity,
            )
            return buyer.response
        try:
            body = orjson.loads(raw_body)
            requested_lines = _requested_lines(body)
            budget_minor = _budget_minor(body)
        except CheckoutTermsRejected as error:
            response = _error(422, error.code, error.content)
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                response,
                started_at,
                "request_rejected",
                buyer=buyer,
            )
            return response
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            response = _error(
                400, "invalid_request", "The checkout request must include line_items."
            )
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                response,
                started_at,
                "request_rejected",
                buyer=buyer,
            )
            return response
        result = await _safe_commerce(
            commerce_service.create_checkout(
                buyer_key_id=buyer.identity.principal_id,
                nonce=buyer.nonce,
                nonce_expires_at=buyer.nonce_expires_at,
                idempotency_key=request.headers["Idempotency-Key"],
                request_sha256=hashlib.sha256(raw_body).hexdigest(),
                requested_lines=requested_lines,
                budget_minor=budget_minor,
            )
        )
        if isinstance(result, JSONResponse):
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                result,
                started_at,
                "unexpected_failure",
                buyer=buyer,
            )
            return result
        response = _mutation_response(
            result, request, merchant_private_key, merchant_key_id, created_status=201
        )
        await _record_exchange(
            protocol_store,
            request,
            raw_body,
            response,
            started_at,
            _mutation_outcome(result, response),
            buyer=buyer,
            checkout_id=result.checkout.id if result.checkout is not None else None,
        )
        return response

    @router.put(f"{SHOPPING_SERVICE_PATH}/checkout-sessions/{{checkout_id}}")
    async def update_checkout(checkout_id: str, request: Request) -> Response:
        started_at = datetime.now(UTC)
        raw_body = await request.body()
        response: Response
        buyer = await _authenticate(
            request, raw_body, base_url, buyer_profile_resolver, protocol_store
        )
        if isinstance(buyer, AuthenticationFailure):
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                buyer.response,
                started_at,
                buyer.outcome,
                identity=buyer.identity,
            )
            return buyer.response
        try:
            body = orjson.loads(raw_body)
            expected_version = _expected_version(body)
            requested_lines = _requested_lines(body)
            budget_minor = _budget_minor(body)
        except CheckoutTermsRejected as error:
            response = _error(422, error.code, error.content)
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                response,
                started_at,
                "request_rejected",
                buyer=buyer,
                checkout_id=checkout_id,
            )
            return response
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            response = _error(400, "invalid_request", "The checkout update is invalid.")
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                response,
                started_at,
                "request_rejected",
                buyer=buyer,
                checkout_id=checkout_id,
            )
            return response
        result = await _safe_commerce(
            commerce_service.update_checkout(
                checkout_id=checkout_id,
                buyer_key_id=buyer.identity.principal_id,
                nonce=buyer.nonce,
                nonce_expires_at=buyer.nonce_expires_at,
                expected_version=expected_version,
                idempotency_key=request.headers["Idempotency-Key"],
                request_sha256=hashlib.sha256(raw_body).hexdigest(),
                requested_lines=requested_lines,
                budget_minor=budget_minor,
            )
        )
        if isinstance(result, JSONResponse):
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                result,
                started_at,
                "unexpected_failure",
                buyer=buyer,
                checkout_id=checkout_id,
            )
            return result
        response = _mutation_response(result, request, merchant_private_key, merchant_key_id)
        await _record_exchange(
            protocol_store,
            request,
            raw_body,
            response,
            started_at,
            _mutation_outcome(result, response),
            buyer=buyer,
            checkout_id=checkout_id,
        )
        return response

    @router.post(f"{SHOPPING_SERVICE_PATH}/checkout-sessions/{{checkout_id}}/cancel")
    @router.delete(f"{SHOPPING_SERVICE_PATH}/checkout-sessions/{{checkout_id}}")
    async def cancel_checkout(checkout_id: str, request: Request) -> Response:
        started_at = datetime.now(UTC)
        raw_body = await request.body()
        response: Response
        buyer = await _authenticate(
            request, raw_body, base_url, buyer_profile_resolver, protocol_store
        )
        if isinstance(buyer, AuthenticationFailure):
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                buyer.response,
                started_at,
                buyer.outcome,
                identity=buyer.identity,
            )
            return buyer.response
        if request.method == "POST" and not raw_body:
            current = await _safe_commerce(
                commerce_service.get_checkout(
                    checkout_id,
                    buyer_key_id=buyer.identity.principal_id,
                )
            )
            if isinstance(current, JSONResponse):
                await _record_exchange(
                    protocol_store,
                    request,
                    raw_body,
                    current,
                    started_at,
                    "unexpected_failure",
                    buyer=buyer,
                    checkout_id=checkout_id,
                )
                return current
            if current is None:
                response = _error(404, "checkout_not_found", "The checkout does not exist.")
                await _record_exchange(
                    protocol_store,
                    request,
                    raw_body,
                    response,
                    started_at,
                    "domain_rejected",
                    buyer=buyer,
                    checkout_id=checkout_id,
                )
                return response
            expected_version = current.version
        else:
            try:
                body = orjson.loads(raw_body)
                expected_version = _expected_version(body)
            except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
                response = _error(
                    400,
                    "invalid_request",
                    "The checkout cancellation is invalid.",
                )
                await _record_exchange(
                    protocol_store,
                    request,
                    raw_body,
                    response,
                    started_at,
                    "request_rejected",
                    buyer=buyer,
                    checkout_id=checkout_id,
                )
                return response
        result = await _safe_commerce(
            commerce_service.cancel_checkout(
                checkout_id=checkout_id,
                buyer_key_id=buyer.identity.principal_id,
                nonce=buyer.nonce,
                nonce_expires_at=buyer.nonce_expires_at,
                expected_version=expected_version,
                idempotency_key=request.headers["Idempotency-Key"],
                request_sha256=hashlib.sha256(raw_body).hexdigest(),
            )
        )
        if isinstance(result, JSONResponse):
            await _record_exchange(
                protocol_store,
                request,
                raw_body,
                result,
                started_at,
                "unexpected_failure",
                buyer=buyer,
                checkout_id=checkout_id,
            )
            return result
        response = _mutation_response(result, request, merchant_private_key, merchant_key_id)
        await _record_exchange(
            protocol_store,
            request,
            raw_body,
            response,
            started_at,
            _mutation_outcome(result, response),
            buyer=buyer,
            checkout_id=checkout_id,
        )
        return response

    @router.get(f"{SHOPPING_SERVICE_PATH}/checkout-sessions/{{checkout_id}}")
    async def get_checkout(checkout_id: str, request: Request) -> Response:
        started_at = datetime.now(UTC)
        response: Response
        buyer = await _authenticate(request, b"", base_url, buyer_profile_resolver, protocol_store)
        if isinstance(buyer, AuthenticationFailure):
            await _record_exchange(
                protocol_store,
                request,
                b"",
                buyer.response,
                started_at,
                buyer.outcome,
                identity=buyer.identity,
            )
            return buyer.response
        checkout = await _safe_commerce(
            commerce_service.get_checkout(
                checkout_id,
                buyer_key_id=buyer.identity.principal_id,
            )
        )
        if isinstance(checkout, JSONResponse):
            response = checkout
            outcome = "unexpected_failure"
        elif checkout is None:
            response = _error(404, "checkout_not_found", "The checkout does not exist.")
            outcome = _outcome(response)
        else:
            response = _signed_response(
                checkout.canonical_bytes, 200, request, merchant_private_key, merchant_key_id
            )
            outcome = _outcome(response)
        await _record_exchange(
            protocol_store,
            request,
            b"",
            response,
            started_at,
            outcome,
            buyer=buyer,
            checkout_id=checkout_id,
        )
        return response

    return router


def _requested_lines(body: object) -> list[RequestedLine]:
    if not isinstance(body, dict):
        raise ValueError
    _validate_currency(body.get("currency"))
    line_items = body["line_items"]
    if not isinstance(line_items, list) or not line_items:
        raise ValueError
    requested: list[RequestedLine] = []
    for line in line_items:
        if not isinstance(line, dict) or not isinstance(line.get("item"), dict):
            raise ValueError
        item = line["item"]
        variant_id = item.get("id")
        quantity = line.get("quantity")
        if not isinstance(variant_id, str) or not variant_id:
            raise ValueError
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError
        _validate_client_terms(line, item)
        requested.append(RequestedLine(variant_id=variant_id, quantity=quantity))
    return requested


def _validate_client_terms(line: dict[object, object], item: dict[object, object]) -> None:
    _validate_currency(line.get("currency"))
    _validate_currency(item.get("currency"))
    for container in (line, item):
        for key in ("unitPriceMinor", "unit_price_minor"):
            value = container.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError
            if value <= 0:
                raise CheckoutTermsRejected(
                    "price_not_positive",
                    "Client-supplied price hints must be positive.",
                )
        unit_price = container.get("unit_price")
        if unit_price is None:
            continue
        if not isinstance(unit_price, dict):
            raise ValueError
        _validate_currency(unit_price.get("currency"))
        minor_units = unit_price.get("minor_units")
        if minor_units is not None:
            if isinstance(minor_units, bool) or not isinstance(minor_units, int):
                raise ValueError
            if minor_units <= 0:
                raise CheckoutTermsRejected(
                    "price_not_positive",
                    "Client-supplied price hints must be positive.",
                )


def _validate_currency(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError
    if value != "INR":
        raise CheckoutTermsRejected(
            "currency_not_supported",
            "Only INR checkout terms are supported.",
        )


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


async def _authenticate(
    request: Request,
    raw_body: bytes,
    base_url: str,
    resolver: BuyerResolver,
    store: UCPProtocolStore,
) -> AuthenticatedBuyer | AuthenticationFailure:
    headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
    signed_request = httpx.Request(
        request.method, f"{base_url}{request.url.path}", headers=headers, content=raw_body
    )
    try:
        key_id = parse_signature_key_id(signed_request)
    except UCPVerificationError:
        return _authentication_failure("signature_rejected")
    try:
        identity = await resolver.resolve(request.headers.get("UCP-Agent", ""), key_id)
    except BuyerProfileError:
        return _authentication_failure("profile_rejected")
    except Exception:
        return _unexpected_failure()
    try:
        existing = await store.get_pin(identity.origin)
        if existing is not None and not _pin_matches(existing, identity):
            raise UCPTrustError("trust_mismatch")
    except UCPTrustError:
        return _authentication_failure("trust_rejected", identity)
    except Exception:
        return _unexpected_failure(identity)
    try:
        verified = verify_request(
            signed_request,
            public_key=identity.public_key,
            expected_key_id=identity.key_id,
        )
    except UCPVerificationError:
        return _authentication_failure("signature_rejected", identity)
    except Exception:
        return _unexpected_failure(identity)
    try:
        await store.verify_or_pin(identity, datetime.now(UTC))
    except UCPTrustError:
        return _authentication_failure("trust_rejected", identity)
    except Exception:
        return _unexpected_failure(identity)
    return AuthenticatedBuyer(identity, verified.nonce, verified.expires_at)


def _authentication_failure(
    outcome: str, identity: BuyerIdentity | None = None
) -> AuthenticationFailure:
    return AuthenticationFailure(
        _error(401, "authentication_failed", "The UCP request signature is invalid."),
        outcome,
        identity,
    )


def _unexpected_failure(identity: BuyerIdentity | None = None) -> AuthenticationFailure:
    return AuthenticationFailure(
        _error(500, "protocol_unavailable", "The checkout service is unavailable."),
        "unexpected_failure",
        identity,
    )


async def _safe_commerce[T](operation: Awaitable[T]) -> T | JSONResponse:
    try:
        return await operation
    except Exception:
        return _error(500, "protocol_unavailable", "The checkout service is unavailable.")


def _pin_matches(pin: TrustPin, identity: BuyerIdentity) -> bool:
    return (
        pin.profile_url == identity.profile_url
        and pin.key_id == identity.key_id
        and pin.fingerprint == identity.fingerprint
        and pin.version == identity.version
    )


def _outcome(response: Response) -> str:
    if 200 <= response.status_code < 300:
        return "accepted"
    if response.status_code == 409:
        return "replay_or_version_rejected"
    return "domain_rejected"


def _mutation_outcome(result: CommerceMutationResult, response: Response) -> str:
    if result.outcome is CommerceMutationOutcome.REPLAYED:
        return "replayed"
    return _outcome(response)


async def _record_exchange(
    store: UCPProtocolStore,
    request: Request,
    raw_body: bytes,
    response: Response,
    started_at: datetime,
    outcome: str,
    *,
    buyer: AuthenticatedBuyer | None = None,
    identity: BuyerIdentity | None = None,
    checkout_id: str | None = None,
) -> None:
    resolved_identity = buyer.identity if buyer is not None else identity
    body = bytes(response.body)
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    if not isinstance(route_path, str):
        route_path = request.url.path
    try:
        await store.append_exchange(
            NewUCPExchange(
                method=request.method,
                route=route_path,
                profile_origin=resolved_identity.origin if resolved_identity is not None else None,
                profile_url_sha256=(
                    hashlib.sha256(resolved_identity.profile_url.encode()).hexdigest()
                    if resolved_identity is not None
                    else None
                ),
                buyer_key_id=resolved_identity.key_id if resolved_identity is not None else None,
                buyer_fingerprint=(
                    resolved_identity.fingerprint if resolved_identity is not None else None
                ),
                nonce_sha256=(
                    hashlib.sha256(buyer.nonce.encode()).hexdigest() if buyer is not None else None
                ),
                request_sha256=hashlib.sha256(raw_body).hexdigest(),
                response_sha256=hashlib.sha256(body).hexdigest() if body else None,
                http_status=response.status_code,
                outcome=outcome,
                checkout_id=checkout_id,
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )
        )
    except Exception:
        return


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
    severity = "recoverable" if status_code in {400, 409, 422} else "unrecoverable"
    return JSONResponse(
        status_code=status_code,
        content={
            "ucp": {"version": UCP_VERSION, "status": "error"},
            "messages": [
                {
                    "type": "error",
                    "code": code,
                    "content": content,
                    "severity": severity,
                }
            ],
        },
    )
