"""Checkout-scoped authorization for the merchant server's browser proxy."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from fastapi import HTTPException, Request


@dataclass(frozen=True, slots=True)
class BrowserIdentity:
    checkout_id: str
    checkout_version: int
    review_at: datetime
    approval_expires_at: datetime
    expires_at: datetime
    csrf_digest: str


class BrowserSessionStorePort(Protocol):
    async def authenticate(self, digest: str) -> BrowserIdentity | None: ...

    async def owns_attempt(self, checkout_id: str, attempt_id: str) -> bool: ...


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BrowserAuthorization:
    def __init__(self, store: BrowserSessionStorePort, merchant_origin: str) -> None:
        self.store = store
        self.merchant_origin = merchant_origin.rstrip("/")

    async def require(
        self, request: Request, *, checkout_id: str | None = None, attempt_id: str | None = None
    ) -> BrowserIdentity:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or not 32 <= len(header[7:]) <= 128:
            raise HTTPException(401, "merchant_session_required")
        identity = await self.store.authenticate(token_digest(header[7:]))
        if identity is None or identity.expires_at <= datetime.now(UTC):
            raise HTTPException(401, "merchant_session_required")
        if checkout_id is not None and identity.checkout_id != checkout_id:
            raise HTTPException(404, "checkout_not_found")
        if attempt_id is not None and not await self.store.owns_attempt(
            identity.checkout_id, attempt_id
        ):
            raise HTTPException(404, "attempt_not_found")
        if request.method not in {"GET", "HEAD"}:
            csrf = request.headers.get("x-csrf-token", "")
            if (
                request.headers.get("origin") != self.merchant_origin
                or not 32 <= len(csrf) <= 128
                or not secrets.compare_digest(identity.csrf_digest, token_digest(csrf))
            ):
                raise HTTPException(403, "merchant_request_rejected")
        return identity


async def require_browser(
    authorization: BrowserAuthorization | None,
    request: Request,
    *,
    checkout_id: str | None = None,
    attempt_id: str | None = None,
) -> BrowserIdentity:
    if authorization is None:
        raise HTTPException(401, "merchant_session_required")
    return await authorization.require(request, checkout_id=checkout_id, attempt_id=attempt_id)
