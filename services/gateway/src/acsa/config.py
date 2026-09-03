from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, PostgresDsn, SecretStr, ValidationError

from acsa.database_urls import has_identity_query_override, has_percent_encoded_database_path
from acsa.security.ucp_signatures import UCPVerificationError, import_public_jwk

REQUIRED_GATEWAY_KEYS = (
    "DATABASE_URL",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "INNGEST_EVENT_KEY",
    "INNGEST_SIGNING_KEY",
    "UCP_BUYER_PUBLIC_JWK",
    "UCP_BUYER_KEY_ID",
    "UCP_MERCHANT_PRIVATE_KEY",
    "UCP_MERCHANT_KEY_ID",
    "PUBLIC_GATEWAY_URL",
    "PUBLIC_MERCHANT_URL",
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
    ucp_buyer_public_jwk: dict[str, Any]
    ucp_buyer_key_id: str
    ucp_merchant_private_key: SecretStr
    ucp_merchant_key_id: str
    public_gateway_url: AnyHttpUrl
    public_merchant_url: AnyHttpUrl


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
                "ucp_buyer_public_jwk": _load_buyer_public_jwk(environment["UCP_BUYER_PUBLIC_JWK"]),
                "ucp_buyer_key_id": environment["UCP_BUYER_KEY_ID"],
                "ucp_merchant_private_key": environment["UCP_MERCHANT_PRIVATE_KEY"],
                "ucp_merchant_key_id": environment["UCP_MERCHANT_KEY_ID"],
                "public_gateway_url": environment["PUBLIC_GATEWAY_URL"],
                "public_merchant_url": environment["PUBLIC_MERCHANT_URL"],
            }
        )
    except ValidationError as error:
        invalid_fields = sorted({str(item["loc"][0]) for item in error.errors()})
        raise ConfigurationError(
            f"Invalid configuration fields: {', '.join(invalid_fields)}"
        ) from None

    if settings.ucp_buyer_public_jwk.get("kid") != settings.ucp_buyer_key_id:
        raise ConfigurationError("Invalid configuration fields: ucp_buyer_key_id")
    if settings.public_gateway_url.scheme != "https":
        raise ConfigurationError("Invalid configuration fields: public_gateway_url")
    if settings.public_merchant_url.scheme != "https":
        raise ConfigurationError("Invalid configuration fields: public_merchant_url")

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


def _load_buyer_public_jwk(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("UCP buyer JWK must be a JSON object")
        import_public_jwk(parsed)
    except (json.JSONDecodeError, UCPVerificationError, ValueError, TypeError):
        raise ConfigurationError("Invalid configuration fields: UCP_BUYER_PUBLIC_JWK") from None
    return parsed
