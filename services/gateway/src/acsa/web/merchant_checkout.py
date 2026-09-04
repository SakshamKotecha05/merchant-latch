"""Token-bound merchant checkout review and approval routes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

import orjson
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from acsa.domain.commerce import ApprovalOutcome
from acsa.security.continue_tokens import ContinueTokenError, verify_continue_token
from acsa.services.commerce import CommerceService


def create_merchant_checkout_router(
    *,
    commerce_service: CommerceService,
    merchant_public_key: ec.EllipticCurvePublicKey,
    clock: Callable[[], datetime] | None = None,
) -> APIRouter:
    router = APIRouter()
    now = clock or (lambda: datetime.now(UTC))

    @router.get("/api/checkouts/{checkout_id}/review")
    async def review_checkout(
        checkout_id: str,
        version: int = Query(gt=0),
        session: str = Query(min_length=1),
    ) -> JSONResponse:
        try:
            claims = verify_continue_token(
                merchant_public_key,
                session,
                checkout_id=checkout_id,
                checkout_version=version,
                now=now(),
            )
        except ContinueTokenError:
            return _error(401, "invalid_continue_session")
        preview = await commerce_service.preview_approval(
            checkout_id=checkout_id,
            expected_version=version,
            approved_at=claims.issued_at,
        )
        if preview.outcome is ApprovalOutcome.NOT_FOUND:
            return _error(404, "checkout_not_found")
        if preview.outcome is ApprovalOutcome.STALE:
            return _error(409, "checkout_version_conflict")
        if preview.outcome is ApprovalOutcome.BLOCKED or preview.snapshot is None:
            code = preview.rule_ids[0] if preview.rule_ids else "checkout_blocked"
            return _error(422, code)
        return JSONResponse(
            {
                "snapshot": preview.snapshot.resource,
                "snapshot_checksum": preview.snapshot.checksum,
                "expires_at": preview.snapshot.expires_at.isoformat().replace("+00:00", "Z"),
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/checkouts/{checkout_id}/approve")
    async def approve_checkout(checkout_id: str, request: Request) -> Response:
        raw_body = await request.body()
        try:
            body = orjson.loads(raw_body)
            version, session_token, checksum = _approval_request(body)
            claims = verify_continue_token(
                merchant_public_key,
                session_token,
                checkout_id=checkout_id,
                checkout_version=version,
                now=now(),
            )
            idempotency_key = request.headers["Idempotency-Key"]
            if not idempotency_key:
                raise ValueError
        except ContinueTokenError:
            return _error(401, "invalid_continue_session")
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            return _error(400, "invalid_approval_request")
        result = await commerce_service.approve_checkout(
            checkout_id=checkout_id,
            expected_version=version,
            snapshot_checksum=checksum,
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            approved_at=claims.issued_at,
        )
        if result.outcome is ApprovalOutcome.CONFLICT:
            return _error(409, "approval_replay_conflict")
        if result.outcome is ApprovalOutcome.STALE:
            return _error(409, "checkout_version_conflict")
        if result.outcome is ApprovalOutcome.NOT_FOUND:
            return _error(404, "checkout_not_found")
        if result.outcome is ApprovalOutcome.BLOCKED:
            code = result.rule_ids[0] if result.rule_ids else "approval_blocked"
            return _error(422, code)
        if result.response_body is None:
            return _error(500, "approval_unavailable")
        return Response(
            content=result.response_body,
            status_code=200,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return router


def _approval_request(body: object) -> tuple[int, str, str]:
    if not isinstance(body, dict):
        raise ValueError
    version = body.get("version")
    session = body.get("session")
    checksum = body.get("snapshot_checksum")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError
    if not isinstance(session, str) or not session:
        raise ValueError
    if not isinstance(checksum, str) or len(checksum) != 64:
        raise ValueError
    return int(version), session, checksum


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code})
