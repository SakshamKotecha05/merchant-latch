from __future__ import annotations

import hashlib
import hmac


def _verify_hex_hmac(*, message: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, signature)
    except TypeError:
        return False


def verify_checkout_signature(
    *,
    stored_order_id: str,
    payment_id: str,
    signature: str,
    key_secret: str,
) -> bool:
    message = f"{stored_order_id}|{payment_id}".encode()
    return _verify_hex_hmac(message=message, signature=signature, secret=key_secret)


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    return _verify_hex_hmac(message=raw_body, signature=signature, secret=secret)
