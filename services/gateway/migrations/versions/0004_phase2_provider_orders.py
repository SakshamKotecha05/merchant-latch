"""Add immutable provider Order records.

Revision ID: 0004_provider_orders
Revises: 0003_phase2
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_provider_orders"
down_revision: str | None = "0003_phase2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("provider_order_id", sa.String(length=128), nullable=False),
        sa.Column("receipt", sa.String(length=40), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recovered", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_provider_order_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_provider_order_currency_inr"),
        sa.ForeignKeyConstraint(["attempt_id"], ["payment_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint("provider_order_id"),
        sa.UniqueConstraint("receipt"),
    )


def downgrade() -> None:
    op.drop_table("provider_orders")
