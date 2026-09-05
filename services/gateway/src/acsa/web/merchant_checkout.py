"""Token-bound merchant checkout review and approval routes."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import orjson
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from acsa.domain.commerce import ApprovalOutcome
from acsa.ports.jobs import JobDispatcherPort
from acsa.security.browser_sessions import BrowserAuthorization, require_browser
from acsa.services.commerce import CommerceService

logger = logging.getLogger(__name__)


def create_merchant_checkout_router(
    *,
    commerce_service: CommerceService,
    merchant_public_key: ec.EllipticCurvePublicKey,
    job_dispatcher: JobDispatcherPort,
    clock: Callable[[], datetime] | None = None,
    authorization: BrowserAuthorization | None = None,
) -> APIRouter:
    router = APIRouter()
    now = clock or (lambda: datetime.now(UTC))

    @router.get("/api/checkouts/{checkout_id}/review")
    async def review_checkout(
        checkout_id: str,
        request: Request,
    ) -> JSONResponse:
        claims = await require_browser(authorization, request, checkout_id=checkout_id)
        if claims.approval_expires_at <= now():
            return _error(409, "approval_expired")
        preview = await commerce_service.preview_approval(
            checkout_id=checkout_id,
            expected_version=claims.checkout_version,
            approved_at=claims.review_at,
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
                "expires_at": min(preview.snapshot.expires_at, claims.approval_expires_at)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/checkouts/{checkout_id}/approve")
    async def approve_checkout(checkout_id: str, request: Request) -> Response:
        claims = await require_browser(authorization, request, checkout_id=checkout_id)
        if claims.approval_expires_at <= now():
            return _error(409, "approval_expired")
        raw_body = await request.body()
        if len(raw_body) > 4096:
            return _error(413, "request_too_large")
        try:
            body = orjson.loads(raw_body)
            checksum = _approval_request(body)
            idempotency_key = request.headers["Idempotency-Key"]
            if not 1 <= len(idempotency_key) <= 128:
                raise ValueError
        except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
            return _error(400, "invalid_approval_request")
        result = await commerce_service.approve_checkout(
            checkout_id=checkout_id,
            expected_version=claims.checkout_version,
            snapshot_checksum=checksum,
            idempotency_key=idempotency_key,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            approved_at=claims.review_at,
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
        if result.outbox_job_id is not None:
            try:
                await job_dispatcher.dispatch(result.outbox_job_id)
            except Exception:
                logger.exception(
                    "Immediate provider-order dispatch failed; scheduled sweep will retry"
                )
        return Response(
            content=result.response_body,
            status_code=200,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return router


def _approval_request(body: object) -> str:
    if not isinstance(body, dict) or set(body) != {"snapshot_checksum", "confirmed"}:
        raise ValueError
    checksum = body.get("snapshot_checksum")
    if body.get("confirmed") is not True:
        raise ValueError
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(c not in "0123456789abcdef" for c in checksum)
    ):
        raise ValueError
    return checksum


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code})
