from __future__ import annotations

import base64
import hashlib
import hmac

from acsa.security.razorpay_signatures import (
    verify_checkout_signature,
    verify_webhook_signature,
)


def test_checkout_signature_binds_server_order_to_payment() -> None:
    secret = "fixture-provider-secret"
    expected = hmac.new(
        secret.encode(),
        b"order_server_stored|pay_fixture",
        hashlib.sha256,
    ).hexdigest()

    assert verify_checkout_signature(
        stored_order_id="order_server_stored",
        payment_id="pay_fixture",
        signature=expected,
        key_secret=secret,
    )
    assert not verify_checkout_signature(
        stored_order_id="order_different",
        payment_id="pay_fixture",
        signature=expected,
        key_secret=secret,
    )


def test_webhook_signature_is_over_exact_raw_bytes() -> None:
    secret = "fixture-webhook-secret"
    raw_body = b'{"event":"payment.captured","payload":{"value":1}}'
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(raw_body, expected, secret)
    assert not verify_webhook_signature(raw_body + b"\n", expected, secret)


def test_webhook_signature_rejects_base64_representation() -> None:
    secret = "fixture-webhook-secret"
    raw_body = b'{"event":"order.paid"}'
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    base64_signature = base64.b64encode(digest).decode()

    assert not verify_webhook_signature(raw_body, base64_signature, secret)
