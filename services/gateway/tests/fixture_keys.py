"""Deterministic, non-production signing keys for the test suite."""

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat


def buyer_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(1, ec.SECP256R1())


def merchant_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(2, ec.SECP256R1())


def fixture_private_key(name: str) -> ec.EllipticCurvePrivateKey:
    keys = {
        "ucp_buyer_private.pem": buyer_private_key,
        "ucp_merchant_private.pem": merchant_private_key,
    }
    try:
        return keys[name]()
    except KeyError:
        raise ValueError("Unknown test key fixture") from None


def private_key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")
