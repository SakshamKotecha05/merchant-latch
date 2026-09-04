from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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
    refund_id: Mapped[str | None] = mapped_column(String(128))
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


class MerchantConfig(Base):
    __tablename__ = "merchant_config"
    __table_args__ = (
        CheckConstraint("currency = 'INR'", name="ck_merchant_currency_inr"),
        CheckConstraint(
            "active_policy_pack_version > 0", name="ck_merchant_policy_version_positive"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_name: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    active_policy_pack_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PolicyPack(Base):
    __tablename__ = "policy_packs"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_policy_pack_version_positive"),
        UniqueConstraint("merchant_id", "version", name="uq_policy_pack_merchant_version"),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchant_config.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PickupLocation(Base):
    __tablename__ = "pickup_locations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchant_config.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    city: Mapped[str] = mapped_column(String(96), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchant_config.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Variant(Base):
    __tablename__ = "variants"
    __table_args__ = (
        CheckConstraint("unit_price_minor > 0", name="ck_variant_price_positive"),
        CheckConstraint("currency = 'INR'", name="ck_variant_currency_inr"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    size: Mapped[str] = mapped_column(String(32), nullable=False)
    color: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_inventory_on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="ck_inventory_reserved_non_negative"),
        CheckConstraint("sold >= 0", name="ck_inventory_sold_non_negative"),
        CheckConstraint("reserved + sold <= on_hand", name="ck_inventory_allocation_within_stock"),
        CheckConstraint("version > 0", name="ck_inventory_version_positive"),
    )

    variant_id: Mapped[str] = mapped_column(
        ForeignKey("variants.id", ondelete="RESTRICT"), primary_key=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CheckoutSession(Base):
    __tablename__ = "checkout_sessions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_checkout_version_positive"),
        CheckConstraint("policy_pack_version > 0", name="ck_checkout_policy_version_positive"),
        CheckConstraint("currency = 'INR'", name="ck_checkout_currency_inr"),
        CheckConstraint(
            "budget_minor IS NULL OR budget_minor >= 0", name="ck_checkout_budget_valid"
        ),
        CheckConstraint(
            "status IN ('open', 'requires_buyer_review', 'approved', 'payment_pending', "
            "'completed', 'canceled', 'expired', 'manual_review')",
            name="ck_checkout_status_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchant_config.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    buyer_key_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    policy_pack_version: Mapped[int] = mapped_column(Integer, nullable=False)
    pickup_location_id: Mapped[str] = mapped_column(
        ForeignKey("pickup_locations.id", ondelete="RESTRICT"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    budget_minor: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CheckoutLine(Base):
    __tablename__ = "checkout_lines"
    __table_args__ = (
        CheckConstraint("position > 0", name="ck_checkout_line_position_positive"),
        CheckConstraint("quantity > 0", name="ck_checkout_line_quantity_positive"),
        CheckConstraint("unit_price_minor > 0", name="ck_checkout_line_price_positive"),
        CheckConstraint(
            "inventory_version > 0", name="ck_checkout_line_inventory_version_positive"
        ),
        UniqueConstraint("checkout_id", "position", name="uq_checkout_line_position"),
        UniqueConstraint("checkout_id", "variant_id", name="uq_checkout_line_variant"),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    checkout_id: Mapped[str] = mapped_column(
        ForeignKey("checkout_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    variant_id: Mapped[str] = mapped_column(
        ForeignKey("variants.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String(128), nullable=False)
    sku: Mapped[str] = mapped_column(String(40), nullable=False)
    size: Mapped[str] = mapped_column(String(32), nullable=False)
    color: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inventory_version: Mapped[int] = mapped_column(Integer, nullable=False)


class ApprovalSnapshotRecord(Base):
    __tablename__ = "approval_snapshots"
    __table_args__ = (
        CheckConstraint("checkout_version > 0", name="ck_snapshot_checkout_version_positive"),
        CheckConstraint("policy_pack_version > 0", name="ck_snapshot_policy_version_positive"),
        UniqueConstraint("checkout_id", "checksum", name="uq_snapshot_checkout_checksum"),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    checkout_id: Mapped[str] = mapped_column(
        ForeignKey("checkout_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    checkout_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_pack_version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    canonical_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        CheckConstraint("attempt_version > 0", name="ck_attempt_version_positive"),
        CheckConstraint("amount_minor > 0", name="ck_attempt_amount_positive"),
        CheckConstraint("currency = 'INR'", name="ck_attempt_currency_inr"),
        CheckConstraint(
            "state IN ('draft', 'provider_order_creating', 'awaiting_payment', 'verifying', "
            "'paid', 'expired', 'failed', 'reconciling', 'paid_inventory_exception', "
            "'refund_pending', 'refunded', 'manual_review', 'canceled')",
            name="ck_attempt_state_valid",
        ),
        UniqueConstraint("checkout_id", "attempt_version", name="uq_attempt_checkout_version"),
        UniqueConstraint(
            "provider_account_id",
            "provider_payment_id",
            name="uq_payment_attempt_provider_payment",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checkout_id: Mapped[str] = mapped_column(
        ForeignKey("checkout_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attempt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    receipt: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("approval_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    provider_uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider_account_id: Mapped[str | None] = mapped_column(String(128))
    provider_payment_id: Mapped[str | None] = mapped_column(String(128))
    payment_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    payment_evidence_source: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProviderOrder(Base):
    __tablename__ = "provider_orders"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_provider_order_amount_positive"),
        CheckConstraint("currency = 'INR'", name="ck_provider_order_currency_inr"),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("payment_attempts.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    provider_order_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    receipt: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    notes: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    recovered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MerchantOrder(Base):
    __tablename__ = "merchant_orders"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_merchant_order_amount_positive"),
        CheckConstraint("currency = 'INR'", name="ck_merchant_order_currency_inr"),
        UniqueConstraint(
            "provider_account_id",
            "provider_payment_id",
            name="uq_merchant_order_provider_payment",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    checkout_id: Mapped[str] = mapped_column(
        ForeignKey("checkout_sessions.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("payment_attempts.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    provider_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_order_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    provider_payment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_refund_amount_positive"),
        CheckConstraint("currency = 'INR'", name="ck_refund_currency_inr"),
        CheckConstraint("status IN ('pending', 'processed')", name="ck_refund_status_valid"),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("payment_attempts.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    provider_refund_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    provider_payment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    receipt: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class InventoryLease(Base):
    __tablename__ = "inventory_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'consumed', 'released', 'expired')",
            name="ck_inventory_lease_state_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("payment_attempts.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CommerceIdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("length(request_sha256) = 64", name="ck_idempotency_digest_length"),
    )

    buyer_key_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    checkout_id: Mapped[str | None] = mapped_column(
        ForeignKey("checkout_sessions.id", ondelete="RESTRICT")
    )
    response_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_audit_event_sequence_positive"),
        UniqueConstraint(
            "aggregate_type", "aggregate_id", "sequence", name="uq_audit_aggregate_sequence"
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_source: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UCPTrustPin(Base):
    __tablename__ = "ucp_trust_pins"
    __table_args__ = (
        CheckConstraint("length(fingerprint) = 64", name="ck_ucp_trust_fingerprint_length"),
    )

    origin: Mapped[str] = mapped_column(String(255), primary_key=True)
    profile_url: Mapped[str] = mapped_column(Text, nullable=False)
    key_id: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ucp_version: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UCPExchangeEvent(Base):
    __tablename__ = "ucp_exchange_events"
    __table_args__ = (
        CheckConstraint("length(request_sha256) = 64", name="ck_ucp_exchange_request_digest"),
        CheckConstraint(
            "response_sha256 IS NULL OR length(response_sha256) = 64",
            name="ck_ucp_exchange_response_digest",
        ),
        CheckConstraint(
            "profile_url_sha256 IS NULL OR length(profile_url_sha256) = 64",
            name="ck_ucp_exchange_profile_digest",
        ),
        CheckConstraint(
            "nonce_sha256 IS NULL OR length(nonce_sha256) = 64",
            name="ck_ucp_exchange_nonce_digest",
        ),
        CheckConstraint(
            "buyer_fingerprint IS NULL OR length(buyer_fingerprint) = 64",
            name="ck_ucp_exchange_buyer_fingerprint",
        ),
        CheckConstraint("http_status BETWEEN 100 AND 599", name="ck_ucp_exchange_http_status"),
        CheckConstraint(
            "method IN ('GET', 'POST', 'PUT', 'DELETE', 'ROTATE')",
            name="ck_ucp_exchange_method",
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'domain_rejected', 'profile_rejected', "
            "'replay_or_version_rejected', 'replayed', 'request_rejected', 'signature_rejected', "
            "'trust_rejected', 'trust_rotated', 'unexpected_failure')",
            name="ck_ucp_exchange_outcome",
        ),
        CheckConstraint(
            "completed_at >= started_at",
            name="ck_ucp_exchange_timestamp_order",
        ),
        Index("ix_ucp_exchange_events_completed_id", "completed_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_origin: Mapped[str | None] = mapped_column(String(255), index=True)
    profile_url_sha256: Mapped[str | None] = mapped_column(String(64))
    buyer_key_id: Mapped[str | None] = mapped_column(String(255))
    buyer_fingerprint: Mapped[str | None] = mapped_column(String(64))
    nonce_sha256: Mapped[str | None] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    checkout_id: Mapped[str | None] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
