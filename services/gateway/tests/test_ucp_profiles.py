from __future__ import annotations

import importlib.util
import ipaddress
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fixture_keys import buyer_private_key

from acsa.security import ucp_signatures
from acsa.security.ucp_signatures import export_public_jwk


def _profiles_module():  # type: ignore[no-untyped-def]
    assert importlib.util.find_spec("acsa.ucp_profiles") is not None
    from acsa import ucp_profiles

    return ucp_profiles


def _private_key() -> ec.EllipticCurvePrivateKey:
    return buyer_private_key()


def _profile(key_id: str = "buyer-p256-2026-01") -> dict[str, object]:
    return {
        "ucp": {
            "version": "2026-04-08",
            "services": {},
            "payment_handlers": {},
        },
        "signing_keys": [export_public_jwk(_private_key().public_key(), key_id=key_id)],
    }


def test_parses_one_quoted_https_profile_parameter() -> None:
    profiles = _profiles_module()

    result = profiles.parse_profile_url('agent=?1, profile="https://Buyer.Example/.well-known/ucp"')

    assert result == "https://buyer.example/.well-known/ucp"


def test_canonicalizes_a_dns_root_dot_to_the_same_origin() -> None:
    profiles = _profiles_module()

    result = profiles.parse_profile_url('profile="https://Buyer.Example./.well-known/ucp"')

    assert result == "https://buyer.example/.well-known/ucp"


def test_canonicalizes_an_origin_only_profile_uri_to_the_root_path() -> None:
    profiles = _profiles_module()

    result = profiles.parse_profile_url('profile="https://buyer.example"')

    assert result == "https://buyer.example/"


def test_accepts_and_returns_one_string_ucp_version_parameter() -> None:
    profiles = _profiles_module()
    header = 'profile="https://buyer.example/.well-known/ucp";version="2026-04-08"'

    assert profiles.parse_profile_url(header) == ("https://buyer.example/.well-known/ucp")
    assert profiles.parse_ucp_agent_version(header) == "2026-04-08"
    assert (
        profiles.parse_ucp_agent_version('profile="https://buyer.example/.well-known/ucp"') is None
    )


@pytest.mark.parametrize(
    "header",
    [
        'profile="https://buyer.example/.well-known/ucp";version',
        'profile="https://buyer.example/.well-known/ucp";version=2026',
        (
            'profile="https://buyer.example/.well-known/ucp";'
            'version="2026-04-08";version="2099-01-01"'
        ),
        'profile="https://buyer.example/.well-known/ucp";unexpected="value"',
    ],
)
def test_rejects_malformed_or_ambiguous_ucp_agent_parameters(header: str) -> None:
    profiles = _profiles_module()

    with pytest.raises(profiles.BuyerProfileError) as caught:
        profiles.parse_ucp_agent_version(header)

    assert caught.value.code == "profile_invalid"


@pytest.mark.parametrize(
    "header",
    [
        "",
        "foo=bar",
        'profile="http://buyer.example/.well-known/ucp"',
        'profile="https://user:pass@buyer.example/.well-known/ucp"',
        'profile="https://buyer.example:8443/.well-known/ucp"',
        'profile="https://127.0.0.1/.well-known/ucp"',
        'profile="https://[::1]/.well-known/ucp"',
        'profile="https://buyer.example/.well-known/ucp#fragment"',
        'profile="https://buyer.example/.well-known/ucp?value=unsafe space"',
        'profile="https://buyer.example/a", profile="https://buyer.example/b"',
    ],
)
def test_rejects_ambiguous_or_unsafe_profile_parameters(header: str) -> None:
    profiles = _profiles_module()

    with pytest.raises(profiles.BuyerProfileError) as caught:
        profiles.parse_profile_url(header)

    assert caught.value.code == "profile_invalid"


def test_validates_profile_and_returns_canonical_identity() -> None:
    profiles = _profiles_module()

    identity = profiles.validate_profile_document(
        "https://buyer.example/.well-known/ucp",
        _profile(),
        "buyer-p256-2026-01",
    )

    assert identity.origin == "https://buyer.example"
    assert identity.key_id == "buyer-p256-2026-01"
    assert identity.version == "2026-04-08"
    assert len(identity.fingerprint) == 64
    assert identity.public_key.public_numbers() == _private_key().public_key().public_numbers()


def test_buyer_principal_is_scoped_beyond_a_reused_key_identifier() -> None:
    first = _profiles_module().validate_profile_document(
        "https://buyer.example/.well-known/ucp",
        _profile(),
        "buyer-p256-2026-01",
    )
    second = _profiles_module().validate_profile_document(
        "https://other-buyer.example/.well-known/ucp",
        _profile(),
        "buyer-p256-2026-01",
    )

    assert first.principal_id != second.principal_id
    assert first.principal_id.startswith("ucp_")


@pytest.mark.parametrize(
    "mutator, expected_code",
    [
        (lambda profile: profile["ucp"].update(version="2026-01-11"), "version_unsupported"),
        (lambda profile: profile.update(signing_keys=[]), "key_not_found"),
        (
            lambda profile: profile["signing_keys"].append(profile["signing_keys"][0].copy()),
            "profile_invalid",
        ),
        (lambda profile: profile["signing_keys"][0].update(d="private"), "key_invalid"),
        (lambda profile: profile["ucp"].pop("services"), "profile_invalid"),
    ],
)
def test_rejects_incompatible_or_ambiguous_profiles(mutator, expected_code: str) -> None:  # type: ignore[no-untyped-def]
    profiles = _profiles_module()
    profile = _profile()
    mutator(profile)

    with pytest.raises(profiles.BuyerProfileError) as caught:
        profiles.validate_profile_document(
            "https://buyer.example/.well-known/ucp",
            profile,
            "buyer-p256-2026-01",
        )

    assert caught.value.code == expected_code


def test_rejects_a_profile_without_the_requested_key() -> None:
    profiles = _profiles_module()

    with pytest.raises(profiles.BuyerProfileError) as caught:
        profiles.validate_profile_document(
            "https://buyer.example/.well-known/ucp",
            _profile("another-key"),
            "buyer-p256-2026-01",
        )

    assert caught.value.code == "key_not_found"


def test_rejects_identity_fields_that_cannot_fit_the_persistent_trust_boundary() -> None:
    profiles = _profiles_module()
    oversized_key_id = "k" * 256

    with pytest.raises(profiles.BuyerProfileError) as url_error:
        profiles.parse_profile_url('profile="https://buyer.example/' + "p" * 2048 + '"')
    with pytest.raises(profiles.BuyerProfileError) as key_error:
        profiles.validate_profile_document(
            "https://buyer.example/.well-known/ucp",
            _profile(oversized_key_id),
            oversized_key_id,
        )

    assert url_error.value.code == "profile_invalid"
    assert key_error.value.code == "profile_invalid"


def test_accepts_optional_jwk_use_and_algorithm_fields_when_absent() -> None:
    profiles = _profiles_module()
    profile = _profile()
    del profile["signing_keys"][0]["use"]
    del profile["signing_keys"][0]["alg"]

    identity = profiles.validate_profile_document(
        "https://buyer.example/.well-known/ucp",
        profile,
        "buyer-p256-2026-01",
    )

    assert identity.key_id == "buyer-p256-2026-01"


@pytest.mark.parametrize(
    "address, expected",
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("192.0.2.1", False),
        ("::1", False),
        ("fe80::1", False),
    ],
)
def test_accepts_only_globally_routable_addresses(address: str, expected: bool) -> None:
    profiles = _profiles_module()

    assert profiles.is_public_address(ipaddress.ip_address(address)) is expected


def test_reads_the_key_identifier_without_accepting_the_signature() -> None:
    request = httpx.Request(
        "GET",
        "https://gateway.example/ucp/shopping/checkout-sessions/chk_1",
        headers={"UCP-Agent": 'profile="https://buyer.example/.well-known/ucp"'},
    )
    now = datetime.now(UTC)
    ucp_signatures.sign_request(
        request,
        private_key=_private_key(),
        key_id="buyer-p256-2026-01",
        created=now,
        expires=now + timedelta(minutes=5),
        nonce="nonce-profile-test",
    )

    assert hasattr(ucp_signatures, "parse_signature_key_id")
    assert ucp_signatures.parse_signature_key_id(request) == "buyer-p256-2026-01"


def test_rejects_an_oversized_claimed_key_identifier_before_profile_fetch() -> None:
    request = httpx.Request(
        "GET",
        "https://gateway.example/ucp/shopping/checkout-sessions/chk_1",
        headers={"UCP-Agent": 'profile="https://buyer.example/.well-known/ucp"'},
    )
    now = datetime.now(UTC)
    ucp_signatures.sign_request(
        request,
        private_key=_private_key(),
        key_id="k" * 256,
        created=now,
        expires=now + timedelta(minutes=5),
        nonce="nonce-profile-test",
    )

    with pytest.raises(ucp_signatures.UCPVerificationError) as caught:
        ucp_signatures.parse_signature_key_id(request)

    assert caught.value.code == "signature_invalid"


def test_rejects_missing_signature_bytes_before_profile_fetch() -> None:
    request = httpx.Request(
        "GET",
        "https://gateway.example/ucp/shopping/checkout-sessions/chk_1",
        headers={"UCP-Agent": 'profile="https://buyer.example/.well-known/ucp"'},
    )
    now = datetime.now(UTC)
    ucp_signatures.sign_request(
        request,
        private_key=_private_key(),
        key_id="buyer-p256-2026-01",
        created=now,
        expires=now + timedelta(minutes=5),
        nonce="nonce-profile-test",
    )
    del request.headers["Signature"]

    with pytest.raises(ucp_signatures.UCPVerificationError) as caught:
        ucp_signatures.parse_signature_key_id(request)

    assert caught.value.code == "signature_missing"
