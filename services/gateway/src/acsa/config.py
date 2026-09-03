from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, PostgresDsn, SecretStr, ValidationError

from acsa.database_urls import has_identity_query_override, has_percent_encoded_database_path

REQUIRED_GATEWAY_KEYS = (
    "DATABASE_URL",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "INNGEST_EVENT_KEY",
    "INNGEST_SIGNING_KEY",
)


class ConfigurationError(RuntimeError):
    """A secret-safe configuration failure."""


class GatewaySettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    database_url: PostgresDsn
    razorpay_key_id: str
    razorpay_key_secret: SecretStr
    razorpay_webhook_secret: SecretStr
    inngest_event_key: SecretStr
    inngest_signing_key: SecretStr


def load_gateway_settings(environment: Mapping[str, str]) -> GatewaySettings:
    missing = [name for name in REQUIRED_GATEWAY_KEYS if not environment.get(name)]
    if missing:
        raise ConfigurationError(f"Missing required configuration: {', '.join(missing)}")

    try:
        settings = GatewaySettings.model_validate(
            {
                "database_url": environment["DATABASE_URL"],
                "razorpay_key_id": environment["RAZORPAY_KEY_ID"],
                "razorpay_key_secret": environment["RAZORPAY_KEY_SECRET"],
                "razorpay_webhook_secret": environment["RAZORPAY_WEBHOOK_SECRET"],
                "inngest_event_key": environment["INNGEST_EVENT_KEY"],
                "inngest_signing_key": environment["INNGEST_SIGNING_KEY"],
            }
        )
    except ValidationError as error:
        invalid_fields = sorted({str(item["loc"][0]) for item in error.errors()})
        raise ConfigurationError(
            f"Invalid configuration fields: {', '.join(invalid_fields)}"
        ) from None

    if has_identity_query_override(settings.database_url):
        raise ConfigurationError(
            "Database connection URLs must not include identity override parameters"
        )
    if has_percent_encoded_database_path(settings.database_url):
        raise ConfigurationError(
            "Database connection URLs must not include percent-encoded database path components"
        )

    database_url = settings.database_url
    if database_url.scheme in {"postgres", "postgresql"}:
        _, separator, connection_details = database_url.unicode_string().partition("://")
        database_url = PostgresDsn(f"postgresql+psycopg{separator}{connection_details}")

    return settings.model_copy(update={"database_url": database_url})
