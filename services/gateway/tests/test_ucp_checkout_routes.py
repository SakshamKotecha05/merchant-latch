from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from fixture_keys import fixture_private_key

from acsa.adapters.postgres.ucp_protocol import NewUCPExchange, TrustPin
from acsa.application import create_application
from acsa.domain.canonical import canonical_json_bytes
from acsa.domain.commerce import (
    AuthoritativeCheckout,
    CheckoutPricing,
    CheckoutStatus,
    CommerceMutationOutcome,
    CommerceMutationResult,
    PricedLine,
)
from acsa.security.ucp_signatures import export_public_jwk, sign_request
from acsa.ucp_profiles import BuyerIdentity, validate_profile_document
from acsa.web.ucp_checkout import create_ucp_checkout_router

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


class NoopStore:
    async def insert_verified_event(self, **_: object) -> object:
        raise AssertionError("Webhook storage is not used by UCP route tests")

    async def mark_processed(self, _: object) -> bool:
        return False


class NoopDispatcher:
    async def dispatch(self, _: object) -> None:
        return None


def _checkout(*, version: int = 1, status: str = "requires_escalation") -> AuthoritativeCheckout:
    resource = {
        "id": "chk_fixed",
        "status": status,
        "checkout_version": version,
        "policy_pack_version": 1,
        "line_items": [
            {
                "item": {
                    "id": "var_stride_42_black",
                    "sku": "ML-STRIDE-BLK-42",
                    "title": "Stride One",
                },
                "quantity": 1,
                "unit_price": {"currency": "INR", "minor_units": 499_900},
                "inventory_version": 3,
            }
        ],
        "pricing": {"currency": "INR", "total_minor": 499_900},
        "fulfillment": {"type": "store_pickup", "location_id": "pickup_blr_01"},
        "continue_url": "https://merchant.example/checkout/chk_fixed",
    }
    line = PricedLine(
        variant_id="var_stride_42_black",
        sku="ML-STRIDE-BLK-42",
        product_name="Stride One",
        size="42",
        color="Black",
        quantity=1,
        unit_price_minor=499_900,
        line_total_minor=499_900,
        inventory_version=3,
    )
    return AuthoritativeCheckout(
        id="chk_fixed",
        buyer_key_id="buyer-p256-2026-01",
        status=(
            CheckoutStatus.CANCELED
            if status == "canceled"
            else CheckoutStatus.REQUIRES_BUYER_REVIEW
        ),
        version=version,
        policy_pack_version=1,
        pickup_location_id="pickup_blr_01",
        lines=(line,),
        pricing=CheckoutPricing("INR", 499_900, 0, 499_900, True),
        budget_minor=None,
        expires_at=NOW + timedelta(minutes=30),
        continue_url="https://merchant.example/checkout/chk_fixed",
        canonical_bytes=canonical_json_bytes(resource),
    )


class MemoryCommerceService:
    def __init__(self) -> None:
        self.checkout = _checkout()
        self.create_calls = 0
        self.lookup_calls: list[dict[str, object]] = []
        self.lookup_result: CommerceMutationResult | None = None
        self.update_kwargs: dict[str, object] | None = None

    async def lookup_idempotency(self, **kwargs: object) -> CommerceMutationResult | None:
        self.lookup_calls.append(kwargs)
        return self.lookup_result

    async def create_checkout(self, **_: object) -> CommerceMutationResult:
        self.create_calls += 1
        return CommerceMutationResult(
            CommerceMutationOutcome.CREATED,
            checkout=self.checkout,
            response_body=self.checkout.canonical_bytes,
        )

    async def update_checkout(self, **kwargs: object) -> CommerceMutationResult:
        self.update_kwargs = kwargs
        self.checkout = _checkout(version=2)
        return CommerceMutationResult(
            CommerceMutationOutcome.UPDATED,
            checkout=self.checkout,
            response_body=self.checkout.canonical_bytes,
        )

    async def cancel_checkout(self, **_: object) -> CommerceMutationResult:
        self.checkout = _checkout(version=2, status="canceled")
        return CommerceMutationResult(
            CommerceMutationOutcome.CANCELED,
            checkout=self.checkout,
            response_body=self.checkout.canonical_bytes,
        )

    async def get_checkout(
        self, checkout_id: str, *, buyer_key_id: str
    ) -> AuthoritativeCheckout | None:
        if checkout_id == self.checkout.id:
            return self.checkout
        return None


class FailingCommerceService(MemoryCommerceService):
    async def create_checkout(self, **_: object) -> CommerceMutationResult:
        raise RuntimeError("sentinel internal failure")


class ReplayingCommerceService(MemoryCommerceService):
    async def create_checkout(self, **_: object) -> CommerceMutationResult:
        return CommerceMutationResult(
            CommerceMutationOutcome.REPLAYED,
            checkout=self.checkout,
            response_body=self.checkout.canonical_bytes,
        )


class MemoryProfileResolver:
    def __init__(self, identity: BuyerIdentity) -> None:
        self.identity = identity

    async def resolve(self, _: str, key_id: str) -> BuyerIdentity:
        assert key_id == self.identity.key_id
        return self.identity


class UncalledProfileResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, _: str, __: str) -> BuyerIdentity:
        self.calls += 1
        raise AssertionError("incompatible versions must not resolve a profile")


class MemoryProtocolStore:
    def __init__(self) -> None:
        self.pin: TrustPin | None = None
        self.exchanges: list[NewUCPExchange] = []

    async def get_pin(self, _: str) -> TrustPin | None:
        return self.pin

    async def verify_or_pin(self, identity: BuyerIdentity, now: datetime) -> TrustPin:
        self.pin = TrustPin(
            origin=identity.origin,
            profile_url=identity.profile_url,
            key_id=identity.key_id,
            fingerprint=identity.fingerprint,
            version=identity.version,
            first_seen_at=now,
            last_seen_at=now,
        )
        return self.pin

    async def append_exchange(self, event: NewUCPExchange) -> object:
        self.exchanges.append(event)
        return object()


def _private_key(name: str) -> ec.EllipticCurvePrivateKey:
    return fixture_private_key(name)


def _client(
    *,
    protocol_store: MemoryProtocolStore | None = None,
    commerce_service: MemoryCommerceService | None = None,
    profile_resolver: MemoryProfileResolver | UncalledProfileResolver | None = None,
) -> TestClient:
    buyer = _private_key("ucp_buyer_private.pem")
    merchant = _private_key("ucp_merchant_private.pem")
    identity = validate_profile_document(
        "https://buyer.example/.well-known/ucp",
        {
            "ucp": {
                "version": "2026-04-08",
                "services": {},
                "payment_handlers": {},
            },
            "signing_keys": [export_public_jwk(buyer.public_key(), key_id="buyer-p256-2026-01")],
        },
        "buyer-p256-2026-01",
    )
    app = create_application(
        webhook_secret="fixture-webhook-secret",
        webhook_store=NoopStore(),
        job_dispatcher=NoopDispatcher(),
    )
    app.include_router(
        create_ucp_checkout_router(
            commerce_service=commerce_service or MemoryCommerceService(),
            buyer_profile_resolver=profile_resolver or MemoryProfileResolver(identity),
            protocol_store=protocol_store or MemoryProtocolStore(),
            merchant_private_key=merchant,
            merchant_key_id="merchant-p256-2026-01",
            public_gateway_url="https://gateway.example",
        )
    )
    return TestClient(app)


def _signed_request(
    method: str = "POST",
    path: str = "/ucp/shopping/checkout-sessions",
    body: dict[str, object] | None = None,
    *,
    idempotency_key: str = "idem-test-01",
    nonce: str = "nonce-test-01",
    ucp_agent: str = 'profile="https://buyer.example/.well-known/ucp"',
) -> httpx.Request:
    content = json.dumps(
        body
        if body is not None
        else {
            "line_items": [
                {
                    "item": {"id": "var_stride_42_black"},
                    "quantity": 1,
                    "unit_price_minor": 1,
                }
            ]
        }
    )
    request = httpx.Request(
        method,
        f"https://gateway.example{path}",
        headers={
            "Content-Type": "application/json",
            "UCP-Agent": ucp_agent,
            "Idempotency-Key": idempotency_key,
        },
        content=content,
    )
    sign_request(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        key_id="buyer-p256-2026-01",
        created=datetime.now(UTC),
        expires=datetime.now(UTC).replace(year=2099),
        nonce=nonce,
    )
    return request


def _send(client: TestClient, request: httpx.Request) -> httpx.Response:
    return client.request(
        request.method,
        request.url.path,
        headers=dict(request.headers),
        content=request.content,
    )


def _signed_empty_request(
    method: str,
    path: str,
    *,
    idempotency_key: str,
    nonce: str,
) -> httpx.Request:
    request = httpx.Request(
        method,
        f"https://gateway.example{path}",
        headers={
            "UCP-Agent": 'profile="https://buyer.example/.well-known/ucp"',
            "Idempotency-Key": idempotency_key,
        },
        content=b"",
    )
    sign_request(
        request,
        private_key=_private_key("ucp_buyer_private.pem"),
        key_id="buyer-p256-2026-01",
        created=datetime.now(UTC),
        expires=datetime.now(UTC).replace(year=2099),
        nonce=nonce,
    )
    return request


def test_discovery_advertises_the_rest_checkout_service() -> None:
    response = _client().get("/.well-known/ucp")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.json()["ucp"]["services"]["dev.ucp.shopping"][0]["endpoint"] == (
        "https://gateway.example/ucp/shopping"
    )
    assert response.json()["signing_keys"][0]["kid"] == "merchant-p256-2026-01"
    assert "keys" not in response.json()


def test_signed_checkout_creation_returns_authoritative_terms() -> None:
    response = _send(_client(), _signed_request())

    assert response.status_code == 201
    assert response.json()["status"] == "requires_escalation"
    assert response.json()["line_items"][0]["unit_price"]["minor_units"] == 499_900
    assert response.json()["continue_url"].startswith("https://merchant.example/")
    assert "signature" in response.headers


def test_incompatible_ucp_version_is_rejected_before_profile_or_commerce_work() -> None:
    protocol_store = MemoryProtocolStore()
    commerce_service = MemoryCommerceService()
    profile_resolver = UncalledProfileResolver()
    request = _signed_request(
        ucp_agent=('profile="https://buyer.example/.well-known/ucp";version="2099-01-01"')
    )

    response = _send(
        _client(
            protocol_store=protocol_store,
            commerce_service=commerce_service,
            profile_resolver=profile_resolver,
        ),
        request,
    )

    assert response.status_code == 422
    assert response.json()["messages"][0]["code"] == "version_unsupported"
    assert profile_resolver.calls == 0
    assert commerce_service.create_calls == 0
    assert protocol_store.exchanges[0].outcome == "request_rejected"


def test_valid_request_establishes_trust_and_records_only_redacted_exchange_data() -> None:
    store = MemoryProtocolStore()

    response = _send(_client(protocol_store=store), _signed_request())

    assert response.status_code == 201
    assert store.pin is not None
    assert store.pin.origin == "https://buyer.example"
    assert len(store.exchanges) == 1
    exchange = store.exchanges[0]
    assert exchange.outcome == "accepted"
    assert exchange.http_status == 201
    assert exchange.profile_origin == "https://buyer.example"
    assert exchange.nonce_sha256 != "nonce-test-01"
    assert not hasattr(exchange, "request_body")
    assert not hasattr(exchange, "signature")


def test_changed_invalid_request_conflicts_before_semantic_validation() -> None:
    commerce_service = MemoryCommerceService()
    commerce_service.lookup_result = CommerceMutationResult(CommerceMutationOutcome.CONFLICT)
    request = _signed_request(
        body={
            "currency": "USD",
            "line_items": [{"item": {"id": "var_stride_42_black"}, "quantity": 1}],
        },
        idempotency_key="idem-existing-01",
        nonce="nonce-conflicting-01",
    )

    response = _send(_client(commerce_service=commerce_service), request)

    assert response.status_code == 409
    assert response.json()["messages"][0]["code"] == "replay_conflict"
    assert commerce_service.create_calls == 0
    assert commerce_service.lookup_calls[0]["operation"] == "create_checkout"


def test_new_invalid_request_never_reaches_a_commerce_mutation() -> None:
    commerce_service = MemoryCommerceService()
    request = _signed_request(
        body={
            "currency": "USD",
            "line_items": [{"item": {"id": "var_stride_42_black"}, "quantity": 1}],
        },
        idempotency_key="idem-unused-01",
        nonce="nonce-invalid-01",
    )

    response = _send(_client(commerce_service=commerce_service), request)

    assert response.status_code == 422
    assert response.json()["messages"][0]["code"] == "currency_not_supported"
    assert commerce_service.create_calls == 0
    assert len(commerce_service.lookup_calls) == 1


def test_trust_change_is_rejected_publicly_and_classified_only_in_redacted_evidence() -> None:
    store = MemoryProtocolStore()
    store.pin = TrustPin(
        origin="https://buyer.example",
        profile_url="https://buyer.example/.well-known/ucp",
        key_id="buyer-p256-2026-01",
        fingerprint="f" * 64,
        version="2026-04-08",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )

    response = _send(_client(protocol_store=store), _signed_request())

    assert response.status_code == 401
    assert response.json() == {
        "ucp": {"version": "2026-04-08", "status": "error"},
        "messages": [
            {
                "type": "error",
                "code": "authentication_failed",
                "content": "The UCP request signature is invalid.",
                "severity": "unrecoverable",
            }
        ],
    }
    assert store.exchanges[0].outcome == "trust_rejected"
    assert store.exchanges[0].profile_origin == "https://buyer.example"


def test_signed_checkout_update_and_cancel_return_current_versions() -> None:
    commerce_service = MemoryCommerceService()
    client = _client(commerce_service=commerce_service)
    update = _signed_request(
        "PUT",
        "/ucp/shopping/checkout-sessions/chk_fixed",
        {
            "expected_version": 1,
            "line_items": [{"item": {"id": "var_stride_42_black"}, "quantity": 1}],
        },
        idempotency_key="idem-update-01",
        nonce="nonce-update-01",
    )
    cancel = _signed_request(
        "DELETE",
        "/ucp/shopping/checkout-sessions/chk_fixed",
        {"expected_version": 2},
        idempotency_key="idem-cancel-01",
        nonce="nonce-cancel-01",
    )

    updated = _send(client, update)
    canceled = _send(client, cancel)

    assert updated.status_code == 200
    assert updated.json()["checkout_version"] == 2
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"
    assert [call["operation"] for call in commerce_service.lookup_calls] == [
        "update_checkout:chk_fixed",
        "cancel_checkout:chk_fixed",
    ]


def test_standard_update_derives_the_current_checkout_version() -> None:
    commerce_service = MemoryCommerceService()
    request = _signed_request(
        "PUT",
        "/ucp/shopping/checkout-sessions/chk_fixed",
        {
            "id": "chk_fixed",
            "currency": "INR",
            "line_items": [{"item": {"id": "var_stride_42_black"}, "quantity": 1}],
        },
        idempotency_key="idem-standard-update-01",
        nonce="nonce-standard-update-01",
    )

    response = _send(_client(commerce_service=commerce_service), request)

    assert response.status_code == 200
    assert commerce_service.update_kwargs is not None
    assert commerce_service.update_kwargs["expected_version"] == 1


def test_ucp_cancel_endpoint_accepts_the_standard_post_without_a_request_body() -> None:
    request = _signed_empty_request(
        "POST",
        "/ucp/shopping/checkout-sessions/chk_fixed/cancel",
        idempotency_key="idem-standard-cancel-01",
        nonce="nonce-standard-cancel-01",
    )

    response = _send(_client(), request)

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"


def test_unexpected_commerce_failures_return_safely_and_leave_redacted_evidence() -> None:
    store = MemoryProtocolStore()

    response = _send(
        _client(protocol_store=store, commerce_service=FailingCommerceService()),
        _signed_request(),
    )

    assert response.status_code == 500
    assert response.json()["ucp"]["status"] == "error"
    assert "sentinel" not in response.text
    assert store.exchanges[0].outcome == "unexpected_failure"


def test_idempotent_replay_is_distinguishable_in_redacted_evidence() -> None:
    store = MemoryProtocolStore()

    response = _send(
        _client(protocol_store=store, commerce_service=ReplayingCommerceService()),
        _signed_request(),
    )

    assert response.status_code == 201
    assert store.exchanges[0].outcome == "replayed"


def test_unsigned_checkout_retrieval_is_rejected() -> None:
    response = _client().get("/ucp/shopping/checkout-sessions/chk_missing")

    assert response.status_code == 401
    assert response.json()["ucp"] == {"version": "2026-04-08", "status": "error"}


def test_merchant_handoff_page_exposes_no_checkout_details() -> None:
    response = _client().get("/checkout/chk_test_01")

    assert response.status_code == 200
    assert "Merchant review required" in response.text
    assert "line_items" not in response.text
