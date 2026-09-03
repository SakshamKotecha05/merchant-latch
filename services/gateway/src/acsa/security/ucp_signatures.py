"""UCP P-256 HTTP Message Signatures and Content-Digest handling."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from http_message_signatures.algorithms import ECDSA_P256_SHA256  # type: ignore[attr-defined]
from http_message_signatures.exceptions import HTTPMessageSignaturesException
from http_message_signatures.http_sfv.dictionary import Dictionary
from http_message_signatures.http_sfv.item import InnerList, Item
from http_message_signatures.resolvers import HTTPSignatureKeyResolver
from http_message_signatures.signatures import HTTPMessageSigner, HTTPMessageVerifier

_REQUEST_BASE_COMPONENTS = ("@method", "@authority", "@path")
_RESPONSE_BODY_COMPONENTS = ("@status", "content-digest", "content-type")
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CLOCK_SKEW_SECONDS = 5
_ERROR_MESSAGES = {
    "algorithm_unsupported": "The signature or digest algorithm is not supported.",
    "components_invalid": "The signature does not cover the required components.",
    "digest_invalid": "The Content-Digest header is invalid.",
    "digest_mismatch": "The Content-Digest does not match the message body.",
    "digest_missing": "The Content-Digest header is required.",
    "key_invalid": "The public signing key is invalid.",
    "key_not_found": "The expected signing key was not used.",
    "signature_expired": "The message signature has expired.",
    "signature_invalid": "The message signature is invalid.",
    "signature_missing": "The signature headers are required.",
}


class UCPVerificationError(ValueError):
    """Safe verification failure for callers at the HTTP boundary."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES.get(code, "Message verification failed."))


class _SingleKeyResolver(HTTPSignatureKeyResolver):
    def __init__(
        self,
        *,
        key_id: str,
        public_key: ec.EllipticCurvePublicKey | None = None,
        private_key: ec.EllipticCurvePrivateKey | None = None,
    ) -> None:
        self._key_id = key_id
        self._public_key = public_key
        self._private_key = private_key

    def resolve_public_key(self, key_id: str) -> ec.EllipticCurvePublicKey:
        if key_id != self._key_id or self._public_key is None:
            raise UCPVerificationError("key_not_found")
        return self._public_key

    def resolve_private_key(self, key_id: str) -> ec.EllipticCurvePrivateKey:
        if key_id != self._key_id or self._private_key is None:
            raise ValueError("The requested private key is unavailable.")
        return self._private_key


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_coordinate(value: object) -> bytes:
    if not isinstance(value, str) or "=" in value:
        raise UCPVerificationError("key_invalid")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise UCPVerificationError("key_invalid") from error
    if len(decoded) != 32 or _encode_base64url(decoded) != value:
        raise UCPVerificationError("key_invalid")
    return decoded


def _validate_public_key(public_key: ec.EllipticCurvePublicKey) -> None:
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise UCPVerificationError("algorithm_unsupported")


def _validate_private_key(private_key: ec.EllipticCurvePrivateKey) -> None:
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise ValueError("A P-256 private key is required.")


def export_public_jwk(
    public_key: ec.EllipticCurvePublicKey,
    *,
    key_id: str,
) -> dict[str, str]:
    """Export a P-256 public key as the UCP JWK shape."""
    _validate_public_key(public_key)
    if not key_id:
        raise ValueError("A key ID is required.")
    numbers = public_key.public_numbers()
    return {
        "kid": key_id,
        "kty": "EC",
        "crv": "P-256",
        "x": _encode_base64url(numbers.x.to_bytes(32, "big")),
        "y": _encode_base64url(numbers.y.to_bytes(32, "big")),
        "use": "sig",
        "alg": "ES256",
    }


def import_public_jwk(jwk: Mapping[str, object]) -> ec.EllipticCurvePublicKey:
    """Import a public-only UCP P-256 JWK."""
    if (
        "d" in jwk
        or jwk.get("kty") != "EC"
        or jwk.get("crv") != "P-256"
        or jwk.get("alg") != "ES256"
    ):
        raise UCPVerificationError("algorithm_unsupported")
    if jwk.get("use") != "sig" or not isinstance(jwk.get("kid"), str) or not jwk["kid"]:
        raise UCPVerificationError("key_invalid")
    x = int.from_bytes(_decode_coordinate(jwk.get("x")), "big")
    y = int.from_bytes(_decode_coordinate(jwk.get("y")), "big")
    try:
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except ValueError as error:
        raise UCPVerificationError("key_invalid") from error


def content_digest(body: bytes) -> str:
    """Return the RFC 9530 SHA-256 digest of exact raw body bytes."""
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    return f"sha-256=:{digest}:"


def validate_content_digest(body: bytes, header: str | None) -> None:
    """Validate a single canonical SHA-256 Content-Digest member."""
    if header is None:
        raise UCPVerificationError("digest_missing")
    if "," in header:
        raise UCPVerificationError("digest_invalid")
    try:
        digest_fields = Dictionary()
        digest_fields.parse(header.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as error:
        raise UCPVerificationError("digest_invalid") from error
    if len(digest_fields) != 1:
        raise UCPVerificationError("digest_invalid")
    algorithm = next(iter(digest_fields))
    if algorithm != "sha-256":
        raise UCPVerificationError("algorithm_unsupported")
    member = digest_fields[algorithm]
    if (
        not isinstance(member, Item)
        or not isinstance(member.value, bytes)
        or member.params
        or len(member.value) != 32
        or str(digest_fields) != header.strip(" ")
    ):
        raise UCPVerificationError("digest_invalid")
    if not hmac.compare_digest(member.value, hashlib.sha256(body).digest()):
        raise UCPVerificationError("digest_mismatch")


def _body_bytes(message: httpx.Request | httpx.Response) -> bytes:
    try:
        return message.content
    except httpx.StreamError as error:
        raise ValueError(
            "The message body must be loaded before signing or verification."
        ) from error


def _request_components(request: httpx.Request, body: bytes) -> tuple[str, ...]:
    components = list(_REQUEST_BASE_COMPONENTS)
    if request.url.query:
        components.append("@query")
    ucp_agent = request.headers.get("UCP-Agent")
    if ucp_agent is None or not ucp_agent.strip():
        raise ValueError("UCP requests require a nonempty UCP-Agent header.")
    components.append("ucp-agent")
    if (
        request.method.upper() in _STATE_CHANGING_METHODS
        and "Idempotency-Key" not in request.headers
    ):
        raise ValueError("State-changing UCP requests require an Idempotency-Key header.")
    if "Idempotency-Key" in request.headers:
        components.append("idempotency-key")
    if body:
        if "Content-Type" not in request.headers:
            raise ValueError("UCP messages with a body require a Content-Type header.")
        components.extend(("content-digest", "content-type"))
    return tuple(components)


def _response_components(response: httpx.Response, body: bytes) -> tuple[str, ...]:
    if not body:
        return ("@status",)
    if "Content-Type" not in response.headers:
        raise ValueError("UCP messages with a body require a Content-Type header.")
    return _RESPONSE_BODY_COMPONENTS


def _validate_signing_inputs(
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str,
    created: datetime,
    expires: datetime,
) -> None:
    _validate_private_key(private_key)
    if not key_id:
        raise ValueError("A key ID is required.")
    if created.tzinfo is None or expires.tzinfo is None:
        raise ValueError("Signature timestamps must be timezone-aware.")
    if expires <= created:
        raise ValueError("The signature expiry must be later than its creation time.")


def _sign(
    message: httpx.Request | httpx.Response,
    *,
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str,
    created: datetime,
    expires: datetime,
    nonce: str | None,
    components: Sequence[str],
) -> None:
    signer = HTTPMessageSigner(
        signature_algorithm=ECDSA_P256_SHA256,
        key_resolver=_SingleKeyResolver(key_id=key_id, private_key=private_key),
    )
    signer.sign(
        message,
        key_id=key_id,
        created=created,
        expires=expires,
        nonce=nonce,
        label="sig1",
        include_alg=False,
        covered_component_ids=components,
    )


def sign_request(
    request: httpx.Request,
    *,
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str,
    created: datetime,
    expires: datetime,
    nonce: str,
) -> None:
    """Add a UCP P-256 signature to an in-memory HTTP request."""
    _validate_signing_inputs(private_key, key_id, created, expires)
    if not nonce:
        raise ValueError("A request signature nonce is required.")
    body = _body_bytes(request)
    components = _request_components(request, body)
    if body:
        request.headers["Content-Digest"] = content_digest(body)
    _sign(
        request,
        private_key=private_key,
        key_id=key_id,
        created=created,
        expires=expires,
        nonce=nonce,
        components=components,
    )


def sign_response(
    response: httpx.Response,
    *,
    private_key: ec.EllipticCurvePrivateKey,
    key_id: str,
    created: datetime,
    expires: datetime,
) -> None:
    """Add a UCP P-256 signature to an in-memory HTTP response."""
    _validate_signing_inputs(private_key, key_id, created, expires)
    body = _body_bytes(response)
    if body:
        response.headers["Content-Digest"] = content_digest(body)
    _sign(
        response,
        private_key=private_key,
        key_id=key_id,
        created=created,
        expires=expires,
        nonce=None,
        components=_response_components(response, body),
    )


def _parse_signature_metadata(
    message: httpx.Request | httpx.Response,
    *,
    expected_key_id: str,
    require_nonce: bool,
) -> tuple[str, ...]:
    if "Signature-Input" not in message.headers or "Signature" not in message.headers:
        raise UCPVerificationError("signature_missing")
    try:
        signature_inputs = Dictionary()
        signature_inputs.parse(message.headers["Signature-Input"].encode("ascii"))
        signatures = Dictionary()
        signatures.parse(message.headers["Signature"].encode("ascii"))
    except (UnicodeEncodeError, ValueError) as error:
        raise UCPVerificationError("signature_invalid") from error
    if list(signature_inputs) != ["sig1"] or list(signatures) != ["sig1"]:
        raise UCPVerificationError("signature_invalid")
    signature_input = signature_inputs["sig1"]
    signature = signatures["sig1"]
    if not isinstance(signature_input, InnerList) or not isinstance(signature, Item):
        raise UCPVerificationError("signature_invalid")
    if not isinstance(signature.value, bytes) or signature.params or len(signature.value) != 64:
        raise UCPVerificationError("signature_invalid")
    components: list[str] = []
    for component in signature_input:
        if type(component.value) is not str or component.params:
            raise UCPVerificationError("components_invalid")
        components.append(component.value)
    parameters = signature_input.params
    if "alg" in parameters:
        raise UCPVerificationError("algorithm_unsupported")
    allowed_parameters = {"created", "expires", "keyid"}
    if require_nonce:
        allowed_parameters.add("nonce")
    if set(parameters) - allowed_parameters:
        raise UCPVerificationError("signature_invalid")
    key_id = parameters.get("keyid")
    if not isinstance(key_id, str):
        raise UCPVerificationError("signature_invalid")
    if key_id != expected_key_id:
        raise UCPVerificationError("key_not_found")
    created = parameters.get("created")
    expires = parameters.get("expires")
    if type(created) is not int or type(expires) is not int:
        raise UCPVerificationError("signature_invalid")
    if expires < time.time() - _CLOCK_SKEW_SECONDS:
        raise UCPVerificationError("signature_expired")
    if expires <= created:
        raise UCPVerificationError("signature_invalid")
    if require_nonce:
        nonce = parameters.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise UCPVerificationError("signature_invalid")
    return tuple(components)


def _verify(
    message: httpx.Request | httpx.Response,
    *,
    public_key: ec.EllipticCurvePublicKey,
    expected_key_id: str,
    expected_components: tuple[str, ...],
    require_nonce: bool,
) -> None:
    _validate_public_key(public_key)
    components = _parse_signature_metadata(
        message,
        expected_key_id=expected_key_id,
        require_nonce=require_nonce,
    )
    if components != expected_components:
        raise UCPVerificationError("components_invalid")
    verifier = HTTPMessageVerifier(
        signature_algorithm=ECDSA_P256_SHA256,
        key_resolver=_SingleKeyResolver(key_id=expected_key_id, public_key=public_key),
    )
    try:
        verifier.verify(message, max_age=timedelta(days=365 * 100))
    except UCPVerificationError:
        raise
    except HTTPMessageSignaturesException as error:
        raise UCPVerificationError("signature_invalid") from error


def verify_request(
    request: httpx.Request,
    *,
    public_key: ec.EllipticCurvePublicKey,
    expected_key_id: str,
) -> None:
    """Verify the digest, UCP metadata, components, key ID, and request signature."""
    body = _body_bytes(request)
    if body:
        validate_content_digest(body, request.headers.get("Content-Digest"))
    try:
        components = _request_components(request, body)
    except ValueError as error:
        raise UCPVerificationError("components_invalid") from error
    _verify(
        request,
        public_key=public_key,
        expected_key_id=expected_key_id,
        expected_components=components,
        require_nonce=True,
    )


def verify_response(
    response: httpx.Response,
    *,
    public_key: ec.EllipticCurvePublicKey,
    expected_key_id: str,
) -> None:
    """Verify the digest, UCP metadata, components, key ID, and response signature."""
    body = _body_bytes(response)
    if body:
        validate_content_digest(body, response.headers.get("Content-Digest"))
    try:
        components = _response_components(response, body)
    except ValueError as error:
        raise UCPVerificationError("components_invalid") from error
    _verify(
        response,
        public_key=public_key,
        expected_key_id=expected_key_id,
        expected_components=components,
        require_nonce=False,
    )
