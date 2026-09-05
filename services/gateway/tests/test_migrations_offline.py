from pathlib import Path

from alembic import command
from alembic.config import Config


def test_remaining_migrations_render_as_transactional_offline_sql(
    monkeypatch,
    capsys,
) -> None:
    gateway_root = Path(__file__).resolve().parents[1]
    config = Config(gateway_root / "alembic.ini")
    config.set_main_option("script_location", str(gateway_root / "migrations"))
    monkeypatch.setenv(
        "DATABASE_DIRECT_URL",
        "postgresql://migration_owner:owner-password@database.example/acsa",
    )

    command.upgrade(config, "0002_phase1:head", sql=True)

    rendered = capsys.readouterr().out
    assert rendered.startswith("BEGIN;")
    assert (
        '\'{"max_quantity_per_line":2,"max_total_quantity":3,'
        '"approval_lifetime_seconds":600,"inventory_lease_lifetime_seconds":600,'
        '"pickup_charge_minor":0,"tax_inclusive":true,'
        '"late_capture_action":"full_refund"}\'::jsonb'
    ) in rendered
    assert "UPDATE alembic_version SET version_num='0009_merchant_browser_sessions'" in rendered
    assert "SET size = 'UK 9'" in rendered
    assert "UPDATE alembic_version SET version_num='0010_demo_catalog_uk9'" in rendered
    assert rendered.rstrip().endswith("COMMIT;")
