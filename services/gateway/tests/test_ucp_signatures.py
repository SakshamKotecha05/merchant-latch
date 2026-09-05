from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from fixture_keys import fixture_private_key
from http_message_signatures import http_sfv

from acsa.security.ucp_signatures import (
    UCPVerificationError,
    content_digest,
    export_public_jwk,
    import_public_jwk,
    sign_request,
    sign_response,
    validate_content_digest,
    verify_request,
    verify_response,
)

FIXTURES = Path(__file__).parent / "fixtures"
REQUEST_COMPONENTS = (
    "@method",
    "@authority",
    "@path",
    "ucp-agent",
    "idempotency-key",
    "content-digest",
    "content-type",
)
RESPONSE_COMPONENTS = ("@status", "content-digest", "content-type")
REQUEST_CREATED = int(datetime(2026, 1, 15, 12, tzinfo=UTC).timestamp())
REQUEST_EXPIRES = int(datetime(2099, 1, 15, 12, tzinfo=UTC).timestamp())
RESPONSE_CREATED = int(datetime(2026, 1, 15, 12, 0, 1, tzinfo=UTC).timestamp())


def _golden_vectors() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((FIXTURES / "ucp_golden_vectors.json").read_text()),
    )


def _private_key(name: str) -> ec.EllipticCurvePrivateKey:
    return fixture_private_key(name)


def _request(vector: dict[str, Any]) -> httpx.Request:
    return httpx.Request(
        vector["method"],
        vector["url"],
        headers=vector["headers"],
        content=bytes.fromhex(vector["body_hex"]),
    )


def _response(vector: dict[str, Any]) -> httpx.Response:
    request = httpx.Request("POST", vector["request_url"])
    return httpx.Response(
        vector["status_code"],
        headers=vector["headers"],
        content=bytes.fromhex(vector["body_hex"]),
        request=request,
    )


def _signature_length(header: str) -> int:
    signatures = http_sfv.Dictionary()
    signatures.parse(header.encode("ascii"))
    value = signatures["sig1"].value
    assert isinstance(value, bytes)
    return len(value)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _serialized_signature_parameters(
    components: Sequence[str],
    parameters: Sequence[tuple[str, int | str]],
) -> str:
    covered = " ".join(_quote(component) for component in components)
    serialized = f"({covered})"
    for name, value in parameters:
        serialized += f";{name}={value if isinstance(value, int) else _quote(value)}"
    return serialized


def _signature_base(
    message: httpx.Request | httpx.Response,
    components: Sequence[str],
    parameters: Sequence[tuple[str, int | str]],
) -> bytes:
    values: list[str] = []
    for component in components:
        if component == "@method":
            assert isinstance(message, httpx.Request)
            value = message.method.upper()
        elif component == "@authority":
            assert isinstance(message, httpx.Request)
            value = message.url.netloc.decode("ascii").lower()
        elif component == "@path":
            assert isinstance(message, httpx.Request)
            value = message.url.path
        elif component == "@status":
            assert isinstance(message, httpx.Response)
            value = str(message.status_code)
        else:
            value = message.headers[component]
        values.append(f"{_quote(component)}: {value}")
    signature_parameters = _serialized_signature_parameters(components, parameters)
    values.append(f'"@signature-params": {signature_parameters}')
    return "\n".join(values).encode("ascii")


def _resign(
    message: httpx.Request | httpx.Response,
    *,
    private_key: ec.EllipticCurvePrivateKey,
    components: Sequence[str],
    parameters: Sequence[tuple[str, int | str]],
) -> None:
    signature_parameters = _serialized_signature_parameters(components, parameters)
    der_signature = private_key.sign(
        _signature_base(message, components, parameters),
        ec.ECDSA(hashes.SHA256()),
    )
    r, s = decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    message.headers["Signature-Input"] = f"sig1={signature_parameters}"
    message.headers["Signature"] = "sig1=:" + base64.b64encode(raw_signature).decode("ascii") + ":"


def _assert_error_code(expected: str, function: Any, *args: Any, **kwargs: Any) -> None:
    with pytest.raises(UCPVerificationError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == expected


def test_public_jwk_round_trip_preserves_p256_key_without_private_material() -> None:
    private_key = _private_key("ucp_buyer_private.pem")

    jwk = export_public_jwk(private_key.public_key(), key_id="buyer-p256-2026-01")
    restored = import_public_jwk(jwk)

    assert jwk == {
        "kid": jwk["kid"],
        "kty": "EC",
        "crv": "P-256",
        "x": jwk["x"],
        "y": jwk["y"],
        "use": "sig",
        "alg": "ES256",
    }
    assert "d" not in jwk
    assert restored.public_numbers() == private_key.public_key().public_numbers()


def test_content_digest_hashes_the_exact_raw_body_bytes() -> None:
    body = b'{"checkout":{"currency":"INR"}}'

    assert content_digest(body) == "sha-256=:2OSUr4ovU7r+l2w3zBLH5BonDH+PutZRJ9zpQqz7SFY=:"
    assert content_digest(body + b"\n") != content_digest(body)


def test_content_digest_validation_rejects_a_digest_mismatch() -> None:
    body = b'{"checkout":{"currency":"INR"}}'

    _assert_error_code(
        "digest_mismatch",
        validate_content_digest,
        body,
        "sha-256=:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=:",
    )


def test_content_digest_validation_rejects_malformed_structured_syntax() -> None:
    _assert_error_code(
        "digest_invalid",
        validate_content_digest,
        b"{}",
        "sha-256=:not-base64!:",
    )


def test_content_digest_validation_rejects_unsupported_algorithms() -> None:
    _assert_error_code(
        "algorithm_unsupported",
        validate_content_digest,
        b"{}",
        "sha-512=:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=:",
    )


def test_content_digest_validation_rejects_multiple_members() -> None:
    _assert_error_code(
        "digest_invalid",
        validate_content_digest,
        b"{}",
        (
            "sha-256=:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=:, "
            "sha-512=:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=:"
        ),
    )


def test_content_digest_validation_rejects_a_missing_digest() -> None:
    _assert_error_code("digest_missing", validate_content_digest, b"{}", None)


def test_stored_buyer_request_golden_vector_verifies_without_regeneration() -> None:
    vector = _golden_vectors()["request"]

    verify_request(
        _request(vector),
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_verified_request_returns_the_signature_nonce_and_expiry() -> None:
    vector = _golden_vectors()["request"]

    verified = verify_request(
        _request(vector),
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )

    assert verified.nonce == "buyer-nonce-phase0-task9"
    assert verified.expires_at == datetime(2099, 1, 15, 12, tzinfo=UTC)


def test_stored_merchant_response_golden_vector_verifies_without_regeneration() -> None:
    vector = _golden_vectors()["response"]

    verify_response(
        _response(vector),
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_stored_raw_p256_signature_verifies_with_cryptography_interoperably() -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    encoded_signature = vector["headers"]["signature"].removeprefix("sig1=:").removesuffix(":")
    raw_signature = base64.b64decode(encoded_signature, validate=True)
    assert len(raw_signature) == 64
    r = int.from_bytes(raw_signature[:32], "big")
    s = int.from_bytes(raw_signature[32:], "big")
    signature_base = _signature_base(
        request,
        REQUEST_COMPONENTS,
        (
            ("created", REQUEST_CREATED),
            ("keyid", vector["key_id"]),
            ("expires", REQUEST_EXPIRES),
            ("nonce", "buyer-nonce-phase0-task9"),
        ),
    )

    import_public_jwk(vector["public_jwk"]).verify(
        encode_dss_signature(r, s),
        signature_base,
        ec.ECDSA(hashes.SHA256()),
    )


def test_request_signing_matches_golden_metadata_and_verifies() -> None:
    vector = _golden_vectors()["request"]
    request = httpx.Request(
        vector["method"],
        vector["url"],
        headers={
            "Content-Type": vector["headers"]["content-type"],
            "UCP-Agent": vector["headers"]["ucp-agent"],
            "Idempotency-Key": vector["headers"]["idempotency-key"],
        },
        content=bytes.fromhex(vector["body_hex"]),
    )

    sign_request(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        key_id=vector["key_id"],
        created=datetime(2026, 1, 15, 12, tzinfo=UTC),
        expires=datetime(2099, 1, 15, 12, tzinfo=UTC),
        nonce="buyer-nonce-phase0-task9",
    )

    assert request.headers["Signature-Input"] == vector["headers"]["signature-input"]
    assert request.headers["Content-Digest"] == vector["headers"]["content-digest"]
    assert _signature_length(request.headers["Signature"]) == 64
    verify_request(
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_response_signing_matches_golden_metadata_and_verifies() -> None:
    vector = _golden_vectors()["response"]
    response = httpx.Response(
        vector["status_code"],
        headers={"Content-Type": vector["headers"]["content-type"]},
        content=bytes.fromhex(vector["body_hex"]),
        request=httpx.Request("POST", vector["request_url"]),
    )

    sign_response(
        response,
        private_key=_private_key("ucp_merchant_private.pem"),
        key_id=vector["key_id"],
        created=datetime(2026, 1, 15, 12, 0, 1, tzinfo=UTC),
        expires=datetime(2099, 1, 15, 12, 0, 1, tzinfo=UTC),
    )

    assert response.headers["Signature-Input"] == vector["headers"]["signature-input"]
    assert response.headers["Content-Digest"] == vector["headers"]["content-digest"]
    assert _signature_length(response.headers["Signature"]) == 64
    verify_response(
        response,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


@pytest.mark.parametrize("profile_header", [None, ""])
def test_request_signing_rejects_missing_or_empty_profile_identity(
    profile_header: str | None,
) -> None:
    vector = _golden_vectors()["request"]
    headers = {
        "Content-Type": vector["headers"]["content-type"],
        "Idempotency-Key": vector["headers"]["idempotency-key"],
    }
    if profile_header is not None:
        headers["UCP-Agent"] = profile_header
    request = httpx.Request(
        vector["method"],
        vector["url"],
        headers=headers,
        content=bytes.fromhex(vector["body_hex"]),
    )

    with pytest.raises(ValueError, match="UCP-Agent"):
        sign_request(
            request,
            private_key=_private_key("ucp_buyer_private.pem"),
            key_id=vector["key_id"],
            created=datetime(2026, 1, 15, 12, tzinfo=UTC),
            expires=datetime(2099, 1, 15, 12, tzinfo=UTC),
            nonce="buyer-nonce-phase0-task9",
        )


@pytest.mark.parametrize("profile_header", [None, ""])
def test_request_verification_rejects_correctly_signed_missing_profile_identity(
    profile_header: str | None,
) -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    if profile_header is None:
        del request.headers["UCP-Agent"]
        components = tuple(
            component for component in REQUEST_COMPONENTS if component != "ucp-agent"
        )
    else:
        request.headers["UCP-Agent"] = profile_header
        components = REQUEST_COMPONENTS
    _resign(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        components=components,
        parameters=(
            ("created", REQUEST_CREATED),
            ("keyid", vector["key_id"]),
            ("expires", REQUEST_EXPIRES),
            ("nonce", "buyer-nonce-phase0-task9"),
        ),
    )

    _assert_error_code(
        "components_invalid",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_request_verification_rejects_a_correctly_signed_uncovered_profile_identity() -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    components = tuple(component for component in REQUEST_COMPONENTS if component != "ucp-agent")
    _resign(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        components=components,
        parameters=(
            ("created", REQUEST_CREATED),
            ("keyid", vector["key_id"]),
            ("expires", REQUEST_EXPIRES),
            ("nonce", "buyer-nonce-phase0-task9"),
        ),
    )

    _assert_error_code(
        "components_invalid",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_request_verification_rejects_a_correctly_signed_missing_expiry() -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    _resign(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        components=REQUEST_COMPONENTS,
        parameters=(
            ("created", REQUEST_CREATED),
            ("keyid", vector["key_id"]),
            ("nonce", "buyer-nonce-phase0-task9"),
        ),
    )

    _assert_error_code(
        "signature_invalid",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_response_verification_rejects_a_correctly_signed_missing_expiry() -> None:
    vector = _golden_vectors()["response"]
    response = _response(vector)
    _resign(
        response,
        private_key=_private_key("ucp_merchant_private.pem"),
        components=RESPONSE_COMPONENTS,
        parameters=(
            ("created", RESPONSE_CREATED),
            ("keyid", vector["key_id"]),
        ),
    )

    _assert_error_code(
        "signature_invalid",
        verify_response,
        response,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


@pytest.mark.parametrize("nonce", [None, ""])
def test_request_verification_rejects_a_correctly_signed_missing_or_empty_nonce(
    nonce: str | None,
) -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    parameters: list[tuple[str, int | str]] = [
        ("created", REQUEST_CREATED),
        ("keyid", vector["key_id"]),
        ("expires", REQUEST_EXPIRES),
    ]
    if nonce is not None:
        parameters.append(("nonce", nonce))
    _resign(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        components=REQUEST_COMPONENTS,
        parameters=parameters,
    )

    _assert_error_code(
        "signature_invalid",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_request_verification_rejects_forbidden_alg_without_reflecting_input() -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    _resign(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        components=REQUEST_COMPONENTS,
        parameters=(
            ("created", REQUEST_CREATED),
            ("keyid", vector["key_id"]),
            ("alg", "ecdsa-p256-sha256"),
            ("expires", REQUEST_EXPIRES),
            ("nonce", "sensitive-input-marker"),
        ),
    )

    with pytest.raises(UCPVerificationError) as caught:
        verify_request(
            request,
            public_key=import_public_jwk(vector["public_jwk"]),
            expected_key_id=vector["key_id"],
        )
    assert caught.value.code == "algorithm_unsupported"
    assert str(caught.value) == "The signature or digest algorithm is not supported."
    assert "sensitive-input-marker" not in str(caught.value)


def test_request_verification_rejects_changed_raw_body_before_signature_acceptance() -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    request._content += b" "

    _assert_error_code(
        "digest_mismatch",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_request_verification_rejects_wrong_covered_components() -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    request.headers["Signature-Input"] = request.headers["Signature-Input"].replace(
        ' "content-type"', ""
    )

    _assert_error_code(
        "components_invalid",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_request_verification_rejects_the_wrong_key_id() -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    request.headers["Signature-Input"] = request.headers["Signature-Input"].replace(
        vector["key_id"], "unexpected-key"
    )

    _assert_error_code(
        "key_not_found",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_request_verification_rejects_an_expired_signature() -> None:
    vector = _golden_vectors()["request"]
    request = httpx.Request(
        vector["method"],
        vector["url"],
        headers={
            "Content-Type": vector["headers"]["content-type"],
            "UCP-Agent": vector["headers"]["ucp-agent"],
            "Idempotency-Key": vector["headers"]["idempotency-key"],
        },
        content=bytes.fromhex(vector["body_hex"]),
    )
    sign_request(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        key_id=vector["key_id"],
        created=datetime(2019, 1, 1, tzinfo=UTC),
        expires=datetime(2020, 1, 1, tzinfo=UTC),
        nonce="expired-request",
    )

    _assert_error_code(
        "signature_expired",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )


def test_request_verification_rejects_a_non_64_byte_raw_signature() -> None:
    vector = _golden_vectors()["request"]
    request = _request(vector)
    request.headers["Signature"] = "sig1=:YWJj:"

    _assert_error_code(
        "signature_invalid",
        verify_request,
        request,
        public_key=import_public_jwk(vector["public_jwk"]),
        expected_key_id=vector["key_id"],
    )
