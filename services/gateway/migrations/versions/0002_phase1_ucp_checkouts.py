"""Create Phase 1 UCP checkout replay tables.

Revision ID: 0002_phase1
Revises: 0001_phase0
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_phase1"
down_revision: str | None = "0001_phase0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ucp_checkouts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("buyer_key_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("continue_url", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resource", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_body", sa.LargeBinary(), nullable=False),
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
        sa.CheckConstraint("status = 'requires_escalation'", name="ck_ucp_checkout_initial_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ucp_checkouts_expires_at", "ucp_checkouts", ["expires_at"])
    op.create_table(
        "ucp_request_nonces",
        sa.Column("buyer_key_id", sa.String(length=255), nullable=False),
        sa.Column("nonce", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checkout_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["checkout_id"], ["ucp_checkouts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("buyer_key_id", "nonce"),
    )
    op.create_index("ix_ucp_request_nonces_expires_at", "ucp_request_nonces", ["expires_at"])
    op.create_table(
        "ucp_idempotency_records",
        sa.Column("buyer_key_id", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("checkout_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["checkout_id"], ["ucp_checkouts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("buyer_key_id", "idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("ucp_idempotency_records")
    op.drop_index("ix_ucp_request_nonces_expires_at", table_name="ucp_request_nonces")
    op.drop_table("ucp_request_nonces")
    op.drop_index("ix_ucp_checkouts_expires_at", table_name="ucp_checkouts")
    op.drop_table("ucp_checkouts")
