"""Token-protected redacted UCP protocol inspection routes."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from acsa.adapters.postgres.ucp_protocol import TrustPin, UCPExchange

_NO_STORE = {"Cache-Control": "no-store"}


class UCPInspectorStore(Protocol):
    async def list_trust_pins(self, *, limit: int, after: str | None) -> tuple[TrustPin, ...]: ...

    async def list_exchanges(
        self, *, limit: int, before: tuple[datetime, UUID] | None
    ) -> tuple[UCPExchange, ...]: ...

    async def get_exchange(self, exchange_id: UUID) -> UCPExchange | None: ...


def create_ucp_inspector_router(
    *,
    store: UCPInspectorStore,
    inspector_token: str,
) -> APIRouter:
    router = APIRouter(prefix="/internal/ucp")

    @router.get("/trust-pins")
    async def list_trust_pins(request: Request) -> JSONResponse:
        unauthorized = _unauthorized(request, inspector_token)
        if unauthorized is not None:
            return unauthorized
        try:
            limit, after = _pin_pagination(request)
        except ValueError:
            return _json({"code": "invalid_pagination"}, status_code=400)
        records = await store.list_trust_pins(limit=limit, after=after)
        next_cursor = {"after": records[-1].origin} if len(records) == limit else None
        return _json(
            {
                "items": [_pin_json(record) for record in records],
                "next": next_cursor,
            }
        )

    @router.get("/exchanges")
    async def list_exchanges(request: Request) -> JSONResponse:
        unauthorized = _unauthorized(request, inspector_token)
        if unauthorized is not None:
            return unauthorized
        try:
            limit, before = _pagination(request)
        except ValueError:
            return _json({"code": "invalid_pagination"}, status_code=400)
        records = await store.list_exchanges(limit=limit, before=before)
        next_cursor = _cursor_json(records[-1]) if len(records) == limit else None
        return _json(
            {
                "items": [_exchange_json(record) for record in records],
                "next": next_cursor,
            }
        )

    @router.get("/exchanges/{exchange_id}")
    async def get_exchange(exchange_id: str, request: Request) -> JSONResponse:
        unauthorized = _unauthorized(request, inspector_token)
        if unauthorized is not None:
            return unauthorized
        try:
            parsed_exchange_id = UUID(exchange_id)
        except ValueError:
            return _json({"code": "invalid_exchange_id"}, status_code=400)
        record = await store.get_exchange(parsed_exchange_id)
        if record is None:
            return _json({"code": "exchange_not_found"}, status_code=404)
        return _json(_exchange_json(record))

    return router


def _unauthorized(request: Request, expected_token: str) -> JSONResponse | None:
    scheme, separator, credential = request.headers.get("Authorization", "").partition(" ")
    if (
        not separator
        or scheme != "Bearer"
        or not secrets.compare_digest(credential.encode(), expected_token.encode())
    ):
        return _json({"code": "authentication_failed"}, status_code=401)
    return None


def _pagination(request: Request) -> tuple[int, tuple[datetime, UUID] | None]:
    query = request.query_params
    if any(len(query.getlist(name)) > 1 for name in ("limit", "before", "before_id")):
        raise ValueError
    limit = _page_limit(query.get("limit", "25"))
    raw_before = query.get("before")
    raw_before_id = query.get("before_id")
    if (raw_before is None) != (raw_before_id is None):
        raise ValueError
    if raw_before is None or raw_before_id is None:
        return limit, None
    try:
        completed_at = datetime.fromisoformat(raw_before.replace("Z", "+00:00"))
        exchange_id = UUID(raw_before_id)
    except ValueError:
        raise ValueError from None
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise ValueError
    return limit, (completed_at, exchange_id)


def _pin_pagination(request: Request) -> tuple[int, str | None]:
    query = request.query_params
    if any(len(query.getlist(name)) > 1 for name in ("limit", "after")):
        raise ValueError
    limit = _page_limit(query.get("limit", "25"))
    after = query.get("after")
    if after is not None and (not after.startswith("https://") or len(after) > 255):
        raise ValueError
    return limit, after


def _page_limit(raw_limit: str) -> int:
    if not raw_limit.isdecimal():
        raise ValueError
    limit = int(raw_limit)
    if not 1 <= limit <= 100:
        raise ValueError
    return limit


def _cursor_json(record: UCPExchange) -> dict[str, str]:
    return {
        "before": record.completed_at.isoformat(),
        "before_id": str(record.id),
    }


def _pin_json(record: TrustPin) -> dict[str, object]:
    return {
        "origin": record.origin,
        "key_id": record.key_id,
        "fingerprint": record.fingerprint,
        "ucp_version": record.version,
        "first_seen_at": record.first_seen_at.isoformat(),
        "last_seen_at": record.last_seen_at.isoformat(),
    }


def _exchange_json(record: UCPExchange) -> dict[str, object]:
    return {
        "id": str(record.id),
        "method": record.method,
        "route": record.route,
        "profile_origin": record.profile_origin,
        "profile_url_sha256": record.profile_url_sha256,
        "buyer_key_id": record.buyer_key_id,
        "buyer_fingerprint": record.buyer_fingerprint,
        "nonce_sha256": record.nonce_sha256,
        "request_sha256": record.request_sha256,
        "response_sha256": record.response_sha256,
        "http_status": record.http_status,
        "outcome": record.outcome,
        "checkout_id": record.checkout_id,
        "started_at": record.started_at.isoformat(),
        "completed_at": record.completed_at.isoformat(),
    }


def _json(content: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status_code, headers=_NO_STORE)
