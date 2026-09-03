from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="razorpay")
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payment_id: Mapped[str | None] = mapped_column(String(128))
    order_id: Mapped[str | None] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxJob(Base):
    __tablename__ = "outbox_jobs"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="ck_outbox_attempt_count_non_negative"),
        CheckConstraint("max_attempts > 0", name="ck_outbox_max_attempts_positive"),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_type: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(96), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    locked_by: Mapped[str | None] = mapped_column(String(128))
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class UCPCheckout(Base):
    __tablename__ = "ucp_checkouts"
    __table_args__ = (
        CheckConstraint("status = 'requires_escalation'", name="ck_ucp_checkout_initial_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    buyer_key_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    continue_url: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    resource: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UCPRequestNonce(Base):
    __tablename__ = "ucp_request_nonces"

    buyer_key_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(255), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    checkout_id: Mapped[str] = mapped_column(
        ForeignKey("ucp_checkouts.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UCPIdempotencyRecord(Base):
    __tablename__ = "ucp_idempotency_records"
    __table_args__ = (
        UniqueConstraint("buyer_key_id", "idempotency_key", name="uq_ucp_idempotency_key"),
    )

    buyer_key_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    checkout_id: Mapped[str] = mapped_column(
        ForeignKey("ucp_checkouts.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
