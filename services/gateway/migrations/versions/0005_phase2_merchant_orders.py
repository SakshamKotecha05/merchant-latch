"""Add payment evidence and immutable merchant Orders.

Revision ID: 0005_merchant_orders
Revises: 0004_provider_orders
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_merchant_orders"
down_revision: str | None = "0004_provider_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment_attempts", sa.Column("provider_account_id", sa.String(128)))
    op.add_column("payment_attempts", sa.Column("provider_payment_id", sa.String(128)))
    op.add_column("payment_attempts", sa.Column("payment_evidence_digest", sa.String(64)))
    op.add_column("payment_attempts", sa.Column("payment_evidence_source", sa.String(32)))
    op.create_unique_constraint(
        "uq_payment_attempt_provider_payment",
        "payment_attempts",
        ["provider_account_id", "provider_payment_id"],
    )
    op.create_table(
        "merchant_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkout_id", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("provider_account_id", sa.String(128), nullable=False),
        sa.Column("provider_order_id", sa.String(128), nullable=False),
        sa.Column("provider_payment_id", sa.String(128), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("evidence_source", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_merchant_order_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_merchant_order_currency_inr"),
        sa.ForeignKeyConstraint(["attempt_id"], ["payment_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkout_sessions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint("checkout_id"),
        sa.UniqueConstraint("provider_order_id"),
        sa.UniqueConstraint(
            "provider_account_id",
            "provider_payment_id",
            name="uq_merchant_order_provider_payment",
        ),
    )


def downgrade() -> None:
    op.drop_table("merchant_orders")
    op.drop_constraint("uq_payment_attempt_provider_payment", "payment_attempts", type_="unique")
    op.drop_column("payment_attempts", "payment_evidence_source")
    op.drop_column("payment_attempts", "payment_evidence_digest")
    op.drop_column("payment_attempts", "provider_payment_id")
    op.drop_column("payment_attempts", "provider_account_id")
