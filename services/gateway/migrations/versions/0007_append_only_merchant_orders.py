"""Guard merchant Orders as append-only records.

Revision ID: 0007_append_only_orders
Revises: 0006_refunds
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_append_only_orders"
down_revision: str | None = "0006_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TRIGGER trg_merchant_orders_append_only "
        "BEFORE UPDATE OR DELETE ON merchant_orders "
        "FOR EACH ROW EXECUTE FUNCTION reject_phase2_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_merchant_orders_append_only ON merchant_orders")
