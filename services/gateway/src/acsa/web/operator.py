"""Single-operator authentication and redacted operational evidence."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from acsa.adapters.postgres.models import (
    AuditEvent,
    CheckoutSession,
    OperatorLoginWindow,
    OperatorSession,
    OutboxJob,
    UCPExchangeEvent,
)
from acsa.security.browser_sessions import token_digest


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=256)


def create_operator_router(
    sessions: async_sessionmaker[AsyncSession],
    merchant_origin: str,
    password_hash: str | None,
) -> APIRouter:
    router = APIRouter(prefix="/internal/merchant")
    origin = merchant_origin.rstrip("/")

    async def authenticate(request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or not 32 <= len(token) <= 128:
            raise HTTPException(401, "operator_session_required")
        digest = token_digest(token)
        async with sessions() as session:
            identity = await session.get(OperatorSession, digest)
            if identity is None or identity.expires_at <= datetime.now(UTC):
                raise HTTPException(401, "operator_session_required")
            if request.method != "GET" and (
                request.headers.get("origin") != origin
                or not secrets.compare_digest(
                    identity.csrf_digest, token_digest(request.headers.get("x-csrf-token", ""))
                )
            ):
                raise HTTPException(403, "operator_request_rejected")
        return digest

    @router.post("/login")
    async def login(request: Request) -> JSONResponse:
        if request.headers.get("origin") != origin:
            raise HTTPException(403, "operator_request_rejected")
        if not password_hash:
            raise HTTPException(503, "operator_not_configured")
        raw_body = await request.body()
        if len(raw_body) > 2048:
            raise HTTPException(400, "invalid_login_request")
        try:
            body = LoginRequest.model_validate_json(raw_body)
        except ValidationError:
            raise HTTPException(400, "invalid_login_request") from None
        now = datetime.now(UTC)
        async with sessions() as session, session.begin():
            await session.execute(
                insert(OperatorLoginWindow)
                .values(id=1, attempts=0, started_at=now)
                .on_conflict_do_nothing()
            )
            window = await session.scalar(
                select(OperatorLoginWindow).where(OperatorLoginWindow.id == 1).with_for_update()
            )
            if window is None:
                raise HTTPException(503, "operator_unavailable")
            if window.started_at + timedelta(minutes=1) <= now:
                window.attempts, window.started_at = 0, now
            if window.attempts >= 5:
                raise HTTPException(429, "login_rate_limited", headers={"Retry-After": "60"})
            window.attempts += 1
        try:
            await run_in_threadpool(
                Argon2id.verify_phc_encoded, body.password.encode(), password_hash
            )
        except (InvalidKey, ValueError):
            raise HTTPException(401, "invalid_credentials") from None
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        async with sessions() as session, session.begin():
            await session.execute(delete(OperatorSession).where(OperatorSession.expires_at <= now))
            session.add(
                OperatorSession(
                    token_digest=token_digest(token),
                    csrf_digest=token_digest(csrf),
                    expires_at=now + timedelta(hours=1),
                )
            )
        return JSONResponse({"session": token, "csrf": csrf}, headers={"Cache-Control": "no-store"})

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        digest = await authenticate(request)
        async with sessions() as session, session.begin():
            await session.execute(
                delete(OperatorSession).where(OperatorSession.token_digest == digest)
            )
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @router.get("/overview")
    async def overview(request: Request) -> JSONResponse:
        await authenticate(request)
        async with sessions() as session:
            checkouts = list(
                await session.scalars(
                    select(CheckoutSession).order_by(CheckoutSession.created_at.desc()).limit(50)
                )
            )
            events = list(
                await session.scalars(
                    select(AuditEvent)
                    .order_by(AuditEvent.created_at.desc(), AuditEvent.sequence.desc())
                    .limit(100)
                )
            )
            exchanges = list(
                await session.scalars(
                    select(UCPExchangeEvent)
                    .order_by(UCPExchangeEvent.completed_at.desc())
                    .limit(50)
                )
            )
            pending = await session.scalar(
                select(func.count())
                .select_from(OutboxJob)
                .where(OutboxJob.completed_at.is_(None), OutboxJob.dead_lettered_at.is_(None))
            )
            dead = await session.scalar(
                select(func.count())
                .select_from(OutboxJob)
                .where(OutboxJob.dead_lettered_at.is_not(None))
            )
            oldest = await session.scalar(
                select(func.min(OutboxJob.created_at)).where(
                    OutboxJob.completed_at.is_(None), OutboxJob.dead_lettered_at.is_(None)
                )
            )
        return JSONResponse(
            {
                "checkouts": [
                    {
                        "id": row.id,
                        "status": row.status,
                        "version": row.version,
                        "policy": row.policy_pack_version,
                    }
                    for row in checkouts
                ],
                "events": [
                    {
                        "aggregate": row.aggregate_id,
                        "type": row.event_type,
                        "source": row.evidence_source,
                        "at": row.created_at.isoformat(),
                    }
                    for row in events
                ],
                "exchanges": [
                    {
                        "method": row.method,
                        "route": row.route,
                        "status": row.http_status,
                        "outcome": row.outcome,
                        "request_digest": row.request_sha256,
                        "response_digest": row.response_sha256,
                    }
                    for row in exchanges
                ],
                "queue": {
                    "pending": pending,
                    "dead_lettered": dead,
                    "oldest_pending_at": None if oldest is None else oldest.isoformat(),
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
