from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, PostgresDsn, SecretStr, ValidationError

from acsa.database_urls import has_identity_query_override, has_percent_encoded_database_path

REQUIRED_GATEWAY_KEYS = (
    "DATABASE_URL",
    "DATABASE_DIRECT_URL",
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
    database_direct_url: PostgresDsn
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
                "database_direct_url": environment["DATABASE_DIRECT_URL"],
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

    if has_identity_query_override(settings.database_url) or has_identity_query_override(
        settings.database_direct_url
    ):
        raise ConfigurationError(
            "Database connection URLs must not include identity override parameters"
        )
    if has_percent_encoded_database_path(
        settings.database_url
    ) or has_percent_encoded_database_path(settings.database_direct_url):
        raise ConfigurationError(
            "Database connection URLs must not include percent-encoded database path components"
        )
    runtime_username = _decoded_url_component(settings.database_url.hosts()[0]["username"])
    owner_username = _decoded_url_component(settings.database_direct_url.hosts()[0]["username"])
    if settings.database_url == settings.database_direct_url or runtime_username == owner_username:
        raise ConfigurationError(
            "DATABASE_URL and DATABASE_DIRECT_URL must use distinct database roles"
        )
    if _database_name(settings.database_url) != _database_name(settings.database_direct_url):
        raise ConfigurationError(
            "DATABASE_URL and DATABASE_DIRECT_URL must identify the same database"
        )

    return settings


def _decoded_url_component(value: str | None) -> str | None:
    return unquote(value) if value is not None else None


def _database_name(url: PostgresDsn) -> str | None:
    path = url.path
    return _decoded_url_component(path.lstrip("/")) if path is not None else None
