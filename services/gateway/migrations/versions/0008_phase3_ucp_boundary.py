"""Add UCP trust pins and redacted exchange evidence.

Revision ID: 0008_phase3_ucp_boundary
Revises: 0007_append_only_orders
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_phase3_ucp_boundary"
down_revision: str | None = "0007_append_only_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ucp_trust_pins",
        sa.Column("origin", sa.String(length=255), nullable=False),
        sa.Column("profile_url", sa.Text(), nullable=False),
        sa.Column("key_id", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("ucp_version", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_ucp_trust_fingerprint_length"),
        sa.PrimaryKeyConstraint("origin"),
    )
    op.create_table(
        "ucp_exchange_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("route", sa.String(length=255), nullable=False),
        sa.Column("profile_origin", sa.String(length=255), nullable=True),
        sa.Column("profile_url_sha256", sa.String(length=64), nullable=True),
        sa.Column("buyer_key_id", sa.String(length=255), nullable=True),
        sa.Column("buyer_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("nonce_sha256", sa.String(length=64), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("checkout_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(request_sha256) = 64", name="ck_ucp_exchange_request_digest"),
        sa.CheckConstraint(
            "response_sha256 IS NULL OR length(response_sha256) = 64",
            name="ck_ucp_exchange_response_digest",
        ),
        sa.CheckConstraint(
            "profile_url_sha256 IS NULL OR length(profile_url_sha256) = 64",
            name="ck_ucp_exchange_profile_digest",
        ),
        sa.CheckConstraint(
            "nonce_sha256 IS NULL OR length(nonce_sha256) = 64",
            name="ck_ucp_exchange_nonce_digest",
        ),
        sa.CheckConstraint(
            "buyer_fingerprint IS NULL OR length(buyer_fingerprint) = 64",
            name="ck_ucp_exchange_buyer_fingerprint",
        ),
        sa.CheckConstraint("http_status BETWEEN 100 AND 599", name="ck_ucp_exchange_http_status"),
        sa.CheckConstraint(
            "method IN ('GET', 'POST', 'PUT', 'DELETE', 'ROTATE')",
            name="ck_ucp_exchange_method",
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'domain_rejected', 'profile_rejected', "
            "'replay_or_version_rejected', 'replayed', 'request_rejected', 'signature_rejected', "
            "'trust_rejected', 'trust_rotated', 'unexpected_failure')",
            name="ck_ucp_exchange_outcome",
        ),
        sa.CheckConstraint(
            "completed_at >= started_at",
            name="ck_ucp_exchange_timestamp_order",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ucp_exchange_events_profile_origin", "ucp_exchange_events", ["profile_origin"]
    )
    op.create_index("ix_ucp_exchange_events_outcome", "ucp_exchange_events", ["outcome"])
    op.create_index("ix_ucp_exchange_events_checkout_id", "ucp_exchange_events", ["checkout_id"])
    op.create_index(
        "ix_ucp_exchange_events_completed_id",
        "ucp_exchange_events",
        ["completed_at", "id"],
    )
    op.execute(
        "CREATE TRIGGER trg_ucp_exchange_events_append_only "
        "BEFORE UPDATE OR DELETE ON ucp_exchange_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_phase2_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ucp_exchange_events_append_only ON ucp_exchange_events")
    op.drop_index("ix_ucp_exchange_events_completed_id", table_name="ucp_exchange_events")
    op.drop_index("ix_ucp_exchange_events_checkout_id", table_name="ucp_exchange_events")
    op.drop_index("ix_ucp_exchange_events_outcome", table_name="ucp_exchange_events")
    op.drop_index("ix_ucp_exchange_events_profile_origin", table_name="ucp_exchange_events")
    op.drop_table("ucp_exchange_events")
    op.drop_table("ucp_trust_pins")
