"""Create the Phase 0 webhook ledger and transactional outbox.

Revision ID: 0001_phase0
Revises: None
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_phase0"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_name", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payment_id", sa.String(length=128), nullable=True),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_table(
        "outbox_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=96), nullable=False),
        sa.Column("aggregate_type", sa.String(length=96), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_attempt_count_non_negative",
        ),
        sa.CheckConstraint("max_attempts > 0", name="ck_outbox_max_attempts_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_jobs_aggregate_id", "outbox_jobs", ["aggregate_id"])
    op.create_index("ix_outbox_jobs_available_at", "outbox_jobs", ["available_at"])
    op.create_index("ix_outbox_jobs_job_type", "outbox_jobs", ["job_type"])


def downgrade() -> None:
    op.drop_index("ix_outbox_jobs_job_type", table_name="outbox_jobs")
    op.drop_index("ix_outbox_jobs_available_at", table_name="outbox_jobs")
    op.drop_index("ix_outbox_jobs_aggregate_id", table_name="outbox_jobs")
    op.drop_table("outbox_jobs")
    op.drop_table("webhook_events")
