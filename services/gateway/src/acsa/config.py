from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, PostgresDsn, SecretStr, ValidationError

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

    if settings.database_url == settings.database_direct_url:
        raise ConfigurationError(
            "DATABASE_URL and DATABASE_DIRECT_URL must use distinct database roles"
        )

    return settings
