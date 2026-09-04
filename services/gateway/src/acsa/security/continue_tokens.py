"""Short-lived P-256 tokens for the merchant checkout handoff."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import orjson
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from acsa.domain.canonical import canonical_json_bytes

_AUDIENCE = "merchant-checkout"
_CLOCK_SKEW = timedelta(seconds=5)
_DEFAULT_LIFETIME = timedelta(minutes=10)
_PUBLIC_ERROR = "Continue session is invalid."


class ContinueTokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ContinueTokenClaims:
    checkout_id: str
    checkout_version: int
    issued_at: datetime
    expires_at: datetime


def issue_continue_token(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    checkout_id: str,
    checkout_version: int,
    now: datetime,
    lifetime: timedelta = _DEFAULT_LIFETIME,
) -> str:
    if not isinstance(private_key.curve, ec.SECP256R1):
        raise ValueError("A P-256 private key is required")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Token time must be timezone-aware")
    if not checkout_id or checkout_version <= 0 or lifetime <= timedelta(0):
        raise ValueError("Token claims are invalid")
    issued_at = now.astimezone(UTC)
    expires_at = issued_at + lifetime
    payload = canonical_json_bytes(
        {
            "aud": _AUDIENCE,
            "checkout_id": checkout_id,
            "checkout_version": checkout_version,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
    )
    digest = hashlib.sha256(payload).digest()
    signature = private_key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_continue_token(
    public_key: ec.EllipticCurvePublicKey,
    token: str,
    *,
    checkout_id: str,
    checkout_version: int,
    now: datetime,
) -> ContinueTokenClaims:
    try:
        if not isinstance(public_key.curve, ec.SECP256R1):
            raise ValueError
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError
        payload_part, signature_part = token.split(".")
        payload = _decode(payload_part)
        signature = _decode(signature_part)
        public_key.verify(
            signature,
            hashlib.sha256(payload).digest(),
            ec.ECDSA(Prehashed(hashes.SHA256())),
        )
        data = orjson.loads(payload)
        if not isinstance(data, dict) or canonical_json_bytes(data) != payload:
            raise ValueError
        if set(data) != {"aud", "checkout_id", "checkout_version", "iat", "exp"}:
            raise ValueError
        issued = data["iat"]
        expires = data["exp"]
        version = data["checkout_version"]
        if type(issued) is not int or type(expires) is not int or type(version) is not int:
            raise ValueError
        if (
            data["aud"] != _AUDIENCE
            or data["checkout_id"] != checkout_id
            or version != checkout_version
        ):
            raise ValueError
        issued_at = datetime.fromtimestamp(issued, UTC)
        expires_at = datetime.fromtimestamp(expires, UTC)
        current = now.astimezone(UTC)
        if issued_at > current + _CLOCK_SKEW or expires_at <= current or expires_at <= issued_at:
            raise ValueError
        return ContinueTokenClaims(
            checkout_id=checkout_id,
            checkout_version=version,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    except (ValueError, TypeError, KeyError, orjson.JSONDecodeError, InvalidSignature):
        raise ContinueTokenError(_PUBLIC_ERROR) from None


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError
    try:
        decoded = base64.b64decode(
            value.encode("ascii") + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ValueError from error
    if _encode(decoded) != value:
        raise ValueError
    return decoded
