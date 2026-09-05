"""Use explicit UK 9 sizing for the demo catalog."""

import sqlalchemy as sa
from alembic import op

revision = "0010_demo_catalog_uk9"
down_revision = "0009_merchant_browser_sessions"
branch_labels = None
depends_on = None

# Keep identifiers stable for existing orders and signed checkout records.
SIZES = {
    "var_stride_41_black": "41",
    "var_stride_42_black": "42",
    "var_court_40_stone": "40",
    "var_court_41_stone": "41",
    "var_trail_42_black": "42",
    "var_trail_43_stone": "43",
}


def _resize(variant_id: str, old_size: str, new_size: str) -> None:
    statement = sa.text(
        "WITH changed AS (UPDATE variants SET size = :new_size "
        "WHERE id = :variant_id AND size = :old_size RETURNING id) "
        "UPDATE inventory SET version = version + 1 "
        "WHERE variant_id IN (SELECT id FROM changed)"
    ).bindparams(new_size=new_size, variant_id=variant_id, old_size=old_size)
    op.execute(str(statement.compile(compile_kwargs={"literal_binds": True})))


def upgrade() -> None:
    for variant_id, old_size in SIZES.items():
        _resize(variant_id, old_size, "UK 9")


def downgrade() -> None:
    for variant_id, old_size in SIZES.items():
        _resize(variant_id, "UK 9", old_size)
