from __future__ import annotations

import httpx
import pytest
import respx

from acsa.adapters.razorpay.client import RazorpayClient, RazorpayProviderError


@pytest.mark.asyncio
@respx.mock
async def test_create_order_sends_one_immutable_attempt_payload() -> None:
    route = respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "order_fixture",
                "entity": "order",
                "amount": 100,
                "amount_paid": 0,
                "amount_due": 100,
                "currency": "INR",
                "receipt": "acsa1_fixture",
                "status": "created",
                "attempts": 0,
                "notes": {
                    "checkout_id": "checkout_01",
                    "attempt_id": "attempt_01",
                    "snapshot_checksum": "checksum_01",
                },
                "created_at": 1_788_000_000,
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = RazorpayClient(
            key_id="rzp_test_fixture",
            key_secret="fixture-secret",
            http_client=http_client,
        )
        order = await client.create_order(
            amount_minor=100,
            currency="INR",
            receipt="acsa1_fixture",
            notes={
                "checkout_id": "checkout_01",
                "attempt_id": "attempt_01",
                "snapshot_checksum": "checksum_01",
            },
        )

    assert order.order_id == "order_fixture"
    assert order.amount_minor == 100
    assert route.called
    request = route.calls[0].request
    assert request.headers["authorization"].startswith("Basic ")
    assert request.read() == (
        b'{"amount":100,"currency":"INR","receipt":"acsa1_fixture",'
        b'"notes":{"checkout_id":"checkout_01","attempt_id":"attempt_01",'
        b'"snapshot_checksum":"checksum_01"},"partial_payment":false}'
    )


@pytest.mark.asyncio
@respx.mock
async def test_receipt_search_uses_server_filter_and_keeps_all_candidates() -> None:
    route = respx.get("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity": "collection",
                "count": 2,
                "items": [
                    {
                        "id": "order_contains",
                        "amount": 100,
                        "currency": "INR",
                        "receipt": "prefix_acsa1_exact",
                        "notes": {},
                    },
                    {
                        "id": "order_exact",
                        "amount": 100,
                        "currency": "INR",
                        "receipt": "acsa1_exact",
                        "notes": {},
                    },
                ],
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = RazorpayClient(
            key_id="rzp_test_fixture",
            key_secret="fixture-secret",
            http_client=http_client,
        )
        orders = await client.fetch_orders_by_receipt("acsa1_exact")

    assert [order.order_id for order in orders] == ["order_contains", "order_exact"]
    assert route.calls[0].request.url.params["receipt"] == "acsa1_exact"


@pytest.mark.asyncio
@respx.mock
async def test_provider_error_exposes_safe_fields_without_response_body() -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"description": "credential detail must not escape"}},
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = RazorpayClient(
            key_id="rzp_test_fixture",
            key_secret="fixture-secret",
            http_client=http_client,
        )
        with pytest.raises(RazorpayProviderError) as caught:
            await client.create_order(
                amount_minor=100,
                currency="INR",
                receipt="acsa1_fixture",
                notes={},
            )

    assert caught.value.status_code == 401
    assert "credential detail" not in str(caught.value)


@pytest.mark.asyncio
@respx.mock
async def test_malformed_success_body_becomes_a_safe_provider_error() -> None:
    respx.post("https://api.razorpay.com/v1/orders").mock(
        return_value=httpx.Response(
            200,
            text="not-json",
            headers={"content-type": "application/json"},
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = RazorpayClient(
            key_id="rzp_test_fixture",
            key_secret="fixture-secret",
            http_client=http_client,
        )
        with pytest.raises(RazorpayProviderError) as caught:
            await client.create_order(
                amount_minor=100,
                currency="INR",
                receipt="acsa1_fixture",
                notes={},
            )

    assert caught.value.status_code == 200
    assert caught.value.operation == "create_order"
    assert "not-json" not in str(caught.value)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_order_returns_the_exact_provider_record() -> None:
    route = respx.get("https://api.razorpay.com/v1/orders/order_fixture").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "order_fixture",
                "amount": 499_900,
                "currency": "INR",
                "receipt": "acsa1_fixture",
                "status": "paid",
                "notes": {"attempt_id": "att_1"},
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = RazorpayClient(
            key_id="rzp_test_fixture",
            key_secret="fixture-secret",
            http_client=http_client,
        )

        order = await client.fetch_order("order_fixture")

    assert order.order_id == "order_fixture"
    assert order.amount_minor == 499_900
    assert order.status == "paid"
    assert route.calls[0].request.url.path == "/v1/orders/order_fixture"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_payment_returns_capture_and_order_authority() -> None:
    route = respx.get("https://api.razorpay.com/v1/payments/pay_fixture").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "pay_fixture",
                "order_id": "order_fixture",
                "amount": 499_900,
                "currency": "INR",
                "status": "captured",
                "captured": True,
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = RazorpayClient(
            key_id="rzp_test_fixture",
            key_secret="fixture-secret",
            http_client=http_client,
        )

        payment = await client.fetch_payment("pay_fixture")

    assert payment.payment_id == "pay_fixture"
    assert payment.order_id == "order_fixture"
    assert payment.amount_minor == 499_900
    assert payment.status == "captured"
    assert payment.captured is True
    assert route.calls[0].request.url.path == "/v1/payments/pay_fixture"


@pytest.mark.asyncio
@respx.mock
async def test_create_full_refund_sends_amount_receipt_and_redacted_notes() -> None:
    route = respx.post("https://api.razorpay.com/v1/payments/pay_fixture/refund").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "rfnd_fixture",
                "payment_id": "pay_fixture",
                "amount": 499_900,
                "currency": "INR",
                "receipt": "acsarfnd1_fixture",
                "status": "processed",
                "notes": {"attempt_id": "att_1", "reason": "inventory_exception"},
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = RazorpayClient(
            key_id="rzp_test_fixture",
            key_secret="fixture-secret",
            http_client=http_client,
        )

        refund = await client.create_full_refund(
            payment_id="pay_fixture",
            amount_minor=499_900,
            receipt="acsarfnd1_fixture",
            notes={"attempt_id": "att_1", "reason": "inventory_exception"},
        )

    assert refund.refund_id == "rfnd_fixture"
    assert route.calls[0].request.read() == (
        b'{"amount":499900,"receipt":"acsarfnd1_fixture",'
        b'"notes":{"attempt_id":"att_1","reason":"inventory_exception"}}'
    )


@pytest.mark.asyncio
@respx.mock
async def test_fetch_refund_returns_provider_status_for_reconciliation() -> None:
    route = respx.get("https://api.razorpay.com/v1/refunds/rfnd_fixture").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "rfnd_fixture",
                "payment_id": "pay_fixture",
                "amount": 499_900,
                "currency": "INR",
                "receipt": "acsarfnd1_fixture",
                "status": "pending",
                "notes": {"attempt_id": "att_1", "reason": "inventory_exception"},
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = RazorpayClient(
            key_id="rzp_test_fixture",
            key_secret="fixture-secret",
            http_client=http_client,
        )

        refund = await client.fetch_refund("rfnd_fixture")

    assert refund.status == "pending"
    assert refund.amount_minor == 499_900
    assert route.called
