from __future__ import annotations

import pytest

from acsa.config import ConfigurationError, load_gateway_settings

REQUIRED_VALUES = {
    "DATABASE_URL": "postgresql+psycopg://runtime-user:runtime-password@db.example/acsa",
    "RAZORPAY_KEY_ID": "rzp_test_fixture_key",
    "RAZORPAY_KEY_SECRET": "fixture-provider-secret",
    "RAZORPAY_WEBHOOK_SECRET": "fixture-webhook-secret",
    "INNGEST_EVENT_KEY": "fixture-event-key",
    "INNGEST_SIGNING_KEY": "signkey-test-fixture",
    "UCP_INSPECTOR_TOKEN": "fixture-inspector-token-at-least-32-characters",
    "UCP_MERCHANT_PRIVATE_KEY": "fixture-merchant-private-key",
    "UCP_MERCHANT_KEY_ID": "merchant-p256-2026-01",
    "PUBLIC_GATEWAY_URL": "https://gateway.example",
    "PUBLIC_MERCHANT_URL": "https://merchant.example",
}


def test_missing_configuration_names_keys_without_leaking_present_values() -> None:
    environment = REQUIRED_VALUES | {"RAZORPAY_KEY_SECRET": "must-never-appear"}
    del environment["DATABASE_URL"]

    with pytest.raises(ConfigurationError) as caught:
        load_gateway_settings(environment)

    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert "must-never-appear" not in message


def test_load_gateway_settings_accepts_complete_runtime_configuration() -> None:
    settings = load_gateway_settings(REQUIRED_VALUES)

    assert settings.razorpay_key_id == "rzp_test_fixture_key"
    assert settings.database_url.hosts()[0]["host"] == "db.example"


def test_gateway_runtime_does_not_require_owner_migration_credentials() -> None:
    environment = REQUIRED_VALUES | {"DATABASE_DIRECT_URL": "not-a-database-url"}

    settings = load_gateway_settings(environment)

    assert settings.database_url.hosts()[0]["username"] == "runtime-user"


def test_gateway_settings_rejects_percent_encoded_database_paths_secret_safely() -> None:
    environment = REQUIRED_VALUES | {
        "DATABASE_URL": REQUIRED_VALUES["DATABASE_URL"].replace("/acsa", "/acsa%2Dledger")
    }

    with pytest.raises(ConfigurationError, match="encoded database path") as caught:
        load_gateway_settings(environment)

    message = str(caught.value)
    assert "runtime-password" not in message
    assert "db.example" not in message
    assert "acsa%2Dledger" not in message


@pytest.mark.parametrize(
    ("environment", "forbidden_value"),
    [
        (
            REQUIRED_VALUES
            | {
                "DATABASE_URL": (
                    "postgresql+psycopg://runtime-user:runtime-password@db.example/acsa?USER=override-user"
                )
            },
            "override-user",
        ),
        (
            REQUIRED_VALUES
            | {
                "DATABASE_URL": (
                    "postgresql+psycopg://runtime-user:runtime-password@db.example/acsa?PaSsWoRd=override-password"
                )
            },
            "override-password",
        ),
        (
            REQUIRED_VALUES
            | {
                "DATABASE_URL": (
                    "postgresql+psycopg://runtime-user:runtime-password@db.example/acsa?database=ledger"
                )
            },
            "ledger",
        ),
    ],
)
def test_gateway_settings_rejects_query_identity_overrides_secret_safely(
    environment: dict[str, str], forbidden_value: str
) -> None:
    with pytest.raises(ConfigurationError, match="identity override") as caught:
        load_gateway_settings(environment)

    assert forbidden_value not in str(caught.value)


def test_gateway_settings_allows_safe_transport_query_parameters() -> None:
    environment = REQUIRED_VALUES | {
        "DATABASE_URL": (
            "postgresql+psycopg://runtime-user:runtime-password@db.example/acsa?"
            "sslmode=require&channel_binding=require"
        )
    }

    assert load_gateway_settings(environment).database_url.query == (
        "sslmode=require&channel_binding=require"
    )


def test_gateway_settings_rejects_short_inspector_token_secret_safely() -> None:
    environment = REQUIRED_VALUES | {"UCP_INSPECTOR_TOKEN": "too-short"}

    with pytest.raises(ConfigurationError, match="ucp_inspector_token") as caught:
        load_gateway_settings(environment)

    assert "too-short" not in str(caught.value)


def test_gateway_settings_rejects_inspector_token_with_outer_whitespace() -> None:
    token = " " + "x" * 32
    environment = REQUIRED_VALUES | {"UCP_INSPECTOR_TOKEN": token}

    with pytest.raises(ConfigurationError, match="ucp_inspector_token") as caught:
        load_gateway_settings(environment)

    assert token not in str(caught.value)


@pytest.mark.parametrize("key", ["rzp_live_example", "unknown", "rzp_test_"])
def test_demo_startup_rejects_non_test_provider_credentials(key):
    with pytest.raises(ConfigurationError, match="razorpay_key_id"):
        load_gateway_settings(REQUIRED_VALUES | {"RAZORPAY_KEY_ID": key})
