"""Exchange signed continuations once; return only scoped merchant state."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from acsa.adapters.postgres.browser_sessions import PostgresBrowserSessionStore
from acsa.security.browser_sessions import BrowserAuthorization, token_digest
from acsa.security.continue_tokens import ContinueTokenError, verify_continue_token


class SessionExchange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    checkout_id: str = Field(min_length=1, max_length=64)
    version: int = Field(gt=0)
    continuation: str = Field(min_length=1, max_length=4096)


def create_merchant_session_router(
    store: PostgresBrowserSessionStore,
    authorization: BrowserAuthorization,
    public_key: ec.EllipticCurvePublicKey,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/merchant/session")
    async def exchange(body: SessionExchange, request: Request) -> JSONResponse:
        if request.headers.get("origin") != authorization.merchant_origin:
            raise HTTPException(403, "merchant_request_rejected")
        now = datetime.now(UTC)
        try:
            claims = verify_continue_token(
                public_key,
                body.continuation,
                checkout_id=body.checkout_id,
                checkout_version=body.version,
                now=now,
            )
        except ContinueTokenError:
            raise HTTPException(401, "invalid_continue_session") from None
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        # Hash signed claims, not the ECDSA signature: equivalent signatures cannot redeem twice.
        redeemed = await store.redeem(
            token_digest=token_digest(token),
            csrf_digest=token_digest(csrf),
            continuation_digest=token_digest(body.continuation.split(".")[0]),
            checkout_id=body.checkout_id,
            checkout_version=body.version,
            now=now,
            approval_expires_at=claims.expires_at,
        )
        if not redeemed:
            raise HTTPException(409, "continuation_unavailable")
        return JSONResponse({"session": token, "csrf": csrf}, headers={"Cache-Control": "no-store"})

    @router.get("/api/checkouts/{checkout_id}/status")
    async def status(checkout_id: str, request: Request) -> JSONResponse:
        await authorization.require(request, checkout_id=checkout_id)
        state = await store.status(checkout_id)
        if state is None:
            raise HTTPException(404, "checkout_not_found")
        return JSONResponse(state, headers={"Cache-Control": "no-store"})

    @router.get("/api/orders/{order_id}")
    async def public_order(order_id: UUID) -> JSONResponse:
        order = await store.public_order(order_id)
        if order is None:
            raise HTTPException(404, "order_not_found")
        return JSONResponse(order, headers={"Cache-Control": "no-store"})

    return router
