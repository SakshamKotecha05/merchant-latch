"""Persist one-time merchant continuation exchanges and scoped sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0009_merchant_browser_sessions"
down_revision = "0008_phase3_ucp_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_sessions",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "operator_login_window",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "merchant_browser_sessions",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column("continuation_digest", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "checkout_id",
            sa.String(64),
            sa.ForeignKey("checkout_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("checkout_version", sa.Integer(), nullable=False),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        sa.Column("review_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_merchant_browser_sessions_checkout_id", "merchant_browser_sessions", ["checkout_id"]
    )
    op.create_index(
        "ix_merchant_browser_sessions_expires_at", "merchant_browser_sessions", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("merchant_browser_sessions")
    op.drop_table("operator_sessions")
    op.drop_table("operator_login_window")
