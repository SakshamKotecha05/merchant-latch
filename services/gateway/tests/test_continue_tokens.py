from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fixture_keys import merchant_private_key

from acsa.security.continue_tokens import (
    ContinueTokenError,
    issue_continue_token,
    verify_continue_token,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _private_key() -> ec.EllipticCurvePrivateKey:
    return merchant_private_key()


def test_continue_token_round_trip_returns_bound_claims() -> None:
    private_key = _private_key()

    token = issue_continue_token(
        private_key,
        checkout_id="chk_1",
        checkout_version=2,
        now=NOW,
        lifetime=timedelta(minutes=10),
    )
    claims = verify_continue_token(
        private_key.public_key(),
        token,
        checkout_id="chk_1",
        checkout_version=2,
        now=NOW + timedelta(minutes=1),
    )

    assert claims.checkout_id == "chk_1"
    assert claims.checkout_version == 2
    assert claims.issued_at == NOW
    assert claims.expires_at == NOW + timedelta(minutes=10)


@pytest.mark.parametrize(
    ("checkout_id", "checkout_version", "now"),
    [
        ("chk_2", 2, NOW),
        ("chk_1", 3, NOW),
        ("chk_1", 2, NOW + timedelta(minutes=10)),
        ("chk_1", 2, NOW - timedelta(seconds=6)),
    ],
)
def test_continue_token_rejects_substitution_expiry_and_future_issue(
    checkout_id: str, checkout_version: int, now: datetime
) -> None:
    private_key = _private_key()
    token = issue_continue_token(
        private_key,
        checkout_id="chk_1",
        checkout_version=2,
        now=NOW,
        lifetime=timedelta(minutes=10),
    )

    with pytest.raises(ContinueTokenError, match="Continue session is invalid"):
        verify_continue_token(
            private_key.public_key(),
            token,
            checkout_id=checkout_id,
            checkout_version=checkout_version,
            now=now,
        )


def test_continue_token_rejects_tampering_with_constant_error() -> None:
    private_key = _private_key()
    token = issue_continue_token(
        private_key,
        checkout_id="chk_1",
        checkout_version=2,
        now=NOW,
    )
    payload, signature = token.split(".")
    tampered = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature

    with pytest.raises(ContinueTokenError, match=r"^Continue session is invalid\.$"):
        verify_continue_token(
            private_key.public_key(),
            tampered,
            checkout_id="chk_1",
            checkout_version=2,
            now=NOW,
        )
