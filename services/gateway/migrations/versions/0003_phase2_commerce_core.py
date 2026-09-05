"""Create the Phase 2 merchant commerce core.

Revision ID: 0003_phase2
Revises: 0002_phase1
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_phase2"
down_revision: str | None = "0002_phase1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_merchant_tables()
    _create_catalog_tables()
    _create_checkout_tables()
    _create_append_only_guards()
    _seed_demo_catalog()


def _create_merchant_tables() -> None:
    op.create_table(
        "merchant_config",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("public_name", sa.String(length=128), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("active_policy_pack_version", sa.Integer(), nullable=False),
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
        sa.CheckConstraint("currency = 'INR'", name="ck_merchant_currency_inr"),
        sa.CheckConstraint(
            "active_policy_pack_version > 0", name="ck_merchant_policy_version_positive"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "policy_packs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_policy_pack_version_positive"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant_config.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("merchant_id", "version", name="uq_policy_pack_merchant_version"),
    )
    op.create_table(
        "pickup_locations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("city", sa.String(length=96), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant_config.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pickup_locations_active", "pickup_locations", ["active"])
    op.create_index("ix_pickup_locations_merchant_id", "pickup_locations", ["merchant_id"])


def _create_catalog_tables() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
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
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant_config.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_products_active", "products", ["active"])
    op.create_index("ix_products_merchant_id", "products", ["merchant_id"])
    op.create_index("ix_products_search_text", "products", ["search_text"])
    op.create_table(
        "variants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("sku", sa.String(length=40), nullable=False),
        sa.Column("size", sa.String(length=32), nullable=False),
        sa.Column("color", sa.String(length=64), nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint("unit_price_minor > 0", name="ck_variant_price_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_variant_currency_inr"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku"),
    )
    op.create_index("ix_variants_active", "variants", ["active"])
    op.create_index("ix_variants_product_id", "variants", ["product_id"])
    op.create_table(
        "inventory",
        sa.Column("variant_id", sa.String(length=64), nullable=False),
        sa.Column("on_hand", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        sa.Column("sold", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("on_hand >= 0", name="ck_inventory_on_hand_non_negative"),
        sa.CheckConstraint("reserved >= 0", name="ck_inventory_reserved_non_negative"),
        sa.CheckConstraint("sold >= 0", name="ck_inventory_sold_non_negative"),
        sa.CheckConstraint(
            "reserved + sold <= on_hand", name="ck_inventory_allocation_within_stock"
        ),
        sa.CheckConstraint("version > 0", name="ck_inventory_version_positive"),
        sa.ForeignKeyConstraint(["variant_id"], ["variants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("variant_id"),
    )


def _create_checkout_tables() -> None:
    op.create_table(
        "checkout_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_key_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy_pack_version", sa.Integer(), nullable=False),
        sa.Column("pickup_location_id", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("budget_minor", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint("version > 0", name="ck_checkout_version_positive"),
        sa.CheckConstraint("policy_pack_version > 0", name="ck_checkout_policy_version_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_checkout_currency_inr"),
        sa.CheckConstraint(
            "budget_minor IS NULL OR budget_minor >= 0", name="ck_checkout_budget_valid"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'requires_buyer_review', 'approved', 'payment_pending', "
            "'completed', 'canceled', 'expired', 'manual_review')",
            name="ck_checkout_status_valid",
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant_config.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["pickup_location_id"], ["pickup_locations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_checkout_sessions_buyer_key_id", "checkout_sessions", ["buyer_key_id"])
    op.create_index("ix_checkout_sessions_expires_at", "checkout_sessions", ["expires_at"])
    op.create_index("ix_checkout_sessions_merchant_id", "checkout_sessions", ["merchant_id"])
    op.create_index("ix_checkout_sessions_status", "checkout_sessions", ["status"])
    op.create_table(
        "checkout_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkout_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("product_name", sa.String(length=128), nullable=False),
        sa.Column("sku", sa.String(length=40), nullable=False),
        sa.Column("size", sa.String(length=32), nullable=False),
        sa.Column("color", sa.String(length=64), nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("inventory_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("position > 0", name="ck_checkout_line_position_positive"),
        sa.CheckConstraint("quantity > 0", name="ck_checkout_line_quantity_positive"),
        sa.CheckConstraint("unit_price_minor > 0", name="ck_checkout_line_price_positive"),
        sa.CheckConstraint(
            "inventory_version > 0", name="ck_checkout_line_inventory_version_positive"
        ),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkout_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["variants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_id", "position", name="uq_checkout_line_position"),
        sa.UniqueConstraint("checkout_id", "variant_id", name="uq_checkout_line_variant"),
    )
    op.create_index("ix_checkout_lines_checkout_id", "checkout_lines", ["checkout_id"])
    op.create_table(
        "approval_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkout_id", sa.String(length=64), nullable=False),
        sa.Column("checkout_version", sa.Integer(), nullable=False),
        sa.Column("policy_pack_version", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("canonical_body", sa.LargeBinary(), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("checkout_version > 0", name="ck_snapshot_checkout_version_positive"),
        sa.CheckConstraint("policy_pack_version > 0", name="ck_snapshot_policy_version_positive"),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkout_sessions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_id", "checksum", name="uq_snapshot_checkout_checksum"),
    )
    op.create_index("ix_approval_snapshots_checkout_id", "approval_snapshots", ["checkout_id"])
    op.create_index("ix_approval_snapshots_checksum", "approval_snapshots", ["checksum"])
    op.create_index("ix_approval_snapshots_expires_at", "approval_snapshots", ["expires_at"])
    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("checkout_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("receipt", sa.String(length=40), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_checksum", sa.String(length=64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider_uncertain", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint("attempt_version > 0", name="ck_attempt_version_positive"),
        sa.CheckConstraint("amount_minor > 0", name="ck_attempt_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_attempt_currency_inr"),
        sa.CheckConstraint(
            "state IN ('draft', 'provider_order_creating', 'awaiting_payment', 'verifying', "
            "'paid', 'expired', 'failed', 'reconciling', 'paid_inventory_exception', "
            "'refund_pending', 'refunded', 'manual_review', 'canceled')",
            name="ck_attempt_state_valid",
        ),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkout_sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["approval_snapshots.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_id", "attempt_version", name="uq_attempt_checkout_version"),
        sa.UniqueConstraint("receipt"),
    )
    op.create_index("ix_payment_attempts_checkout_id", "payment_attempts", ["checkout_id"])
    op.create_index("ix_payment_attempts_state", "payment_attempts", ["state"])
    op.create_table(
        "inventory_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('active', 'consumed', 'released', 'expired')",
            name="ck_inventory_lease_state_valid",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["payment_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
    )
    op.create_index("ix_inventory_leases_expires_at", "inventory_leases", ["expires_at"])
    op.create_index("ix_inventory_leases_state", "inventory_leases", ["state"])
    op.create_table(
        "idempotency_records",
        sa.Column("buyer_key_id", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("checkout_id", sa.String(length=64), nullable=True),
        sa.Column("response_body", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(request_sha256) = 64", name="ck_idempotency_digest_length"),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkout_sessions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("buyer_key_id", "operation", "idempotency_key"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_source", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence > 0", name="ck_audit_event_sequence_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "aggregate_type", "aggregate_id", "sequence", name="uq_audit_aggregate_sequence"
        ),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])


def _create_append_only_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_phase2_append_only_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only record cannot be mutated';
        END;
        $$
        """
    )
    for table in ("policy_packs", "approval_snapshots", "audit_events"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_phase2_append_only_mutation()"
        )


def _seed_demo_catalog() -> None:
    op.execute(
        """
        INSERT INTO merchant_config (id, public_name, currency, active_policy_pack_version)
        VALUES ('merchant_demo', 'MerchantLatch', 'INR', 1)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        r"""
        INSERT INTO policy_packs (id, merchant_id, version, rules)
        VALUES (
            '00000000-0000-4000-8000-000000000001', 'merchant_demo', 1,
            '{"max_quantity_per_line"\:2,"max_total_quantity"\:3,"approval_lifetime_seconds"\:600,"inventory_lease_lifetime_seconds"\:600,"pickup_charge_minor"\:0,"tax_inclusive"\:true,"late_capture_action"\:"full_refund"}'::jsonb
        )
        ON CONFLICT (merchant_id, version) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO pickup_locations (id, merchant_id, name, city, active)
        VALUES ('pickup_blr_01', 'merchant_demo', 'MerchantLatch Bengaluru', 'Bengaluru', true)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO products
            (id, merchant_id, name, description, search_text, active)
        VALUES
            (
                'prod_stride', 'merchant_demo', 'Stride One',
                'A clean everyday sneaker.', 'stride one clean everyday sneaker', true
            ),
            (
                'prod_court', 'merchant_demo', 'Court Low',
                'A low-profile court sneaker.', 'court low profile sneaker', true
            ),
            (
                'prod_trail', 'merchant_demo', 'Trail Form',
                'A stable sneaker for mixed terrain.',
                'trail form stable mixed terrain sneaker', true
            )
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO variants
            (id, product_id, sku, size, color, unit_price_minor, currency, active)
        VALUES
            (
                'var_stride_41_black', 'prod_stride', 'ML-STRIDE-BLK-41',
                '41', 'Black', 499900, 'INR', true
            ),
            (
                'var_stride_42_black', 'prod_stride', 'ML-STRIDE-BLK-42',
                '42', 'Black', 499900, 'INR', true
            ),
            (
                'var_court_40_stone', 'prod_court', 'ML-COURT-STN-40',
                '40', 'Stone', 549900, 'INR', true
            ),
            (
                'var_court_41_stone', 'prod_court', 'ML-COURT-STN-41',
                '41', 'Stone', 549900, 'INR', true
            ),
            (
                'var_trail_42_black', 'prod_trail', 'ML-TRAIL-BLK-42',
                '42', 'Black', 599900, 'INR', true
            ),
            (
                'var_trail_43_stone', 'prod_trail', 'ML-TRAIL-STN-43',
                '43', 'Stone', 599900, 'INR', true
            )
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO inventory (variant_id, on_hand, reserved, sold, version)
        SELECT id, 8, 0, 0, 1 FROM variants
        WHERE id IN (
            'var_stride_41_black', 'var_stride_42_black', 'var_court_40_stone',
            'var_court_41_stone', 'var_trail_42_black', 'var_trail_43_stone'
        )
        ON CONFLICT (variant_id) DO NOTHING
        """
    )


def downgrade() -> None:
    for table in ("audit_events", "approval_snapshots", "policy_packs"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_phase2_append_only_mutation()")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("idempotency_records")
    op.drop_index("ix_inventory_leases_state", table_name="inventory_leases")
    op.drop_index("ix_inventory_leases_expires_at", table_name="inventory_leases")
    op.drop_table("inventory_leases")
    op.drop_index("ix_payment_attempts_state", table_name="payment_attempts")
    op.drop_index("ix_payment_attempts_checkout_id", table_name="payment_attempts")
    op.drop_table("payment_attempts")
    op.drop_index("ix_approval_snapshots_expires_at", table_name="approval_snapshots")
    op.drop_index("ix_approval_snapshots_checksum", table_name="approval_snapshots")
    op.drop_index("ix_approval_snapshots_checkout_id", table_name="approval_snapshots")
    op.drop_table("approval_snapshots")
    op.drop_index("ix_checkout_lines_checkout_id", table_name="checkout_lines")
    op.drop_table("checkout_lines")
    op.drop_index("ix_checkout_sessions_status", table_name="checkout_sessions")
    op.drop_index("ix_checkout_sessions_merchant_id", table_name="checkout_sessions")
    op.drop_index("ix_checkout_sessions_expires_at", table_name="checkout_sessions")
    op.drop_index("ix_checkout_sessions_buyer_key_id", table_name="checkout_sessions")
    op.drop_table("checkout_sessions")
    op.drop_table("inventory")
    op.drop_index("ix_variants_product_id", table_name="variants")
    op.drop_index("ix_variants_active", table_name="variants")
    op.drop_table("variants")
    op.drop_index("ix_products_search_text", table_name="products")
    op.drop_index("ix_products_merchant_id", table_name="products")
    op.drop_index("ix_products_active", table_name="products")
    op.drop_table("products")
    op.drop_index("ix_pickup_locations_merchant_id", table_name="pickup_locations")
    op.drop_index("ix_pickup_locations_active", table_name="pickup_locations")
    op.drop_table("pickup_locations")
    op.drop_table("policy_packs")
    op.drop_table("merchant_config")
