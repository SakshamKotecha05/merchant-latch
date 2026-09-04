"""Add idempotent full-refund records.

Revision ID: 0006_refunds
Revises: 0005_merchant_orders
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_refunds"
down_revision: str | None = "0005_merchant_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("refund_id", sa.String(128)))
    op.create_table(
        "refunds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("provider_refund_id", sa.String(128), nullable=False),
        sa.Column("provider_payment_id", sa.String(128), nullable=False),
        sa.Column("receipt", sa.String(40), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_refund_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_refund_currency_inr"),
        sa.CheckConstraint("status IN ('pending', 'processed')", name="ck_refund_status_valid"),
        sa.ForeignKeyConstraint(["attempt_id"], ["payment_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint("provider_refund_id"),
        sa.UniqueConstraint("receipt"),
    )


def downgrade() -> None:
    op.drop_table("refunds")
    op.drop_column("webhook_events", "refund_id")
