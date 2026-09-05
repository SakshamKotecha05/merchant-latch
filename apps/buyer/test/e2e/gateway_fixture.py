from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

from acsa.adapters.postgres.ucp_protocol import NewUCPExchange, TrustPin
from acsa.domain.canonical import canonical_json_bytes
from acsa.domain.commerce import (
    AuthoritativeCheckout,
    CheckoutPricing,
    CheckoutStatus,
    CommerceMutationOutcome,
    CommerceMutationResult,
    PricedLine,
)
from acsa.ucp_profiles import BuyerIdentity, BuyerProfileResolver, ProfileHTTPResponse
from acsa.web.ucp_checkout import create_ucp_checkout_router
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI

GATEWAY_ORIGIN = os.environ.get("E2E_GATEWAY_ORIGIN", "https://gateway.example")
BUYER_PROFILE_URL = "https://buyer.example/.well-known/ucp"
VARIANT_ID = "var_stride_42_black"
merchant_key = ec.generate_private_key(ec.SECP256R1())
merchant_key_id = "merchant-e2e-key"
inventory_quantity = 3


async def resolve_dns(_: str) -> tuple[str, ...]:
    return ("8.8.8.8",)


async def fetch_buyer_profile(**_: object) -> ProfileHTTPResponse:
    document = {
        "ucp": {
            "version": "2026-04-08",
            "services": {},
            "payment_handlers": {},
        },
        "signing_keys": [json.loads(os.environ["E2E_BUYER_JWK"])],
    }
    return ProfileHTTPResponse(
        200,
        {"content-type": "application/json", "cache-control": "public, max-age=300"},
        json.dumps(document, separators=(",", ":")).encode(),
    )


class ProtocolStore:
    def __init__(self) -> None:
        self.pin: TrustPin | None = None
        self.exchanges: list[NewUCPExchange] = []

    async def get_pin(self, _: str) -> TrustPin | None:
        return self.pin

    async def verify_or_pin(self, identity: BuyerIdentity, now: datetime) -> TrustPin:
        if self.pin is None:
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


class CommerceService:
    def __init__(self) -> None:
        self.requests: dict[str, str] = {}
        self.responses: dict[str, bytes] = {}
        self.checkout: AuthoritativeCheckout | None = None
        self.calls = 0

    async def lookup_idempotency(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        **_: object,
    ) -> CommerceMutationResult | None:
        existing_digest = self.requests.get(idempotency_key)
        if existing_digest is None:
            return None
        if existing_digest != request_sha256:
            return CommerceMutationResult(CommerceMutationOutcome.CONFLICT)
        return CommerceMutationResult(
            CommerceMutationOutcome.REPLAYED,
            checkout=self.checkout,
            response_body=self.responses[idempotency_key],
        )

    async def create_checkout(self, **values: object) -> CommerceMutationResult:
        self.calls += 1
        idempotency_key = str(values["idempotency_key"])
        request_sha256 = str(values["request_sha256"])
        outcome = CommerceMutationOutcome.CREATED
        if idempotency_key in self.requests:
            if self.requests[idempotency_key] != request_sha256:
                return CommerceMutationResult(CommerceMutationOutcome.CONFLICT)
            outcome = CommerceMutationOutcome.REPLAYED
        self.requests[idempotency_key] = request_sha256
        expires_at = datetime.now(UTC) + timedelta(minutes=30)
        line = PricedLine(
            variant_id=VARIANT_ID,
            sku="STRIDE-42-BLK",
            product_name="Stride Runner",
            size="42",
            color="Black",
            quantity=1,
            unit_price_minor=249_900,
            line_total_minor=249_900,
            inventory_version=5,
        )
        resource = {
            "ucp": {
                "version": "2026-04-08",
                "capabilities": {
                    "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}]
                },
                "payment_handlers": {},
            },
            "id": "chk_e2e",
            "status": "requires_escalation",
            "currency": "INR",
            "line_items": [
                {
                    "id": VARIANT_ID,
                    "item": {
                        "id": VARIANT_ID,
                        "title": "Stride Runner",
                        "price": 249_900,
                    },
                    "quantity": 1,
                    "totals": [
                        {"type": "subtotal", "amount": 249_900},
                        {"type": "total", "amount": 249_900},
                    ],
                }
            ],
            "totals": [
                {"type": "subtotal", "amount": 249_900},
                {"type": "total", "amount": 249_900},
            ],
            "links": [],
            "messages": [
                {
                    "type": "error",
                    "code": "merchant_review_required",
                    "content": "Continue in the merchant checkout.",
                    "severity": "requires_buyer_review",
                }
            ],
            "continue_url": f"{GATEWAY_ORIGIN}/checkout/chk_e2e",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }
        body = canonical_json_bytes(resource)
        checkout = AuthoritativeCheckout(
            id="chk_e2e",
            buyer_key_id=str(values["buyer_key_id"]),
            status=CheckoutStatus.REQUIRES_BUYER_REVIEW,
            version=1,
            policy_pack_version=1,
            pickup_location_id="pickup_1",
            lines=(line,),
            pricing=CheckoutPricing("INR", 249_900, 0, 249_900, True),
            budget_minor=300_000,
            expires_at=expires_at,
            continue_url=f"{GATEWAY_ORIGIN}/checkout/chk_e2e",
            canonical_bytes=body,
        )
        self.checkout = checkout
        self.responses[idempotency_key] = body
        return CommerceMutationResult(outcome, checkout=checkout, response_body=body)


store = ProtocolStore()
commerce = CommerceService()
resolver = BuyerProfileResolver(
    dns_resolver=resolve_dns,
    fetch_hop=fetch_buyer_profile,
)
app = FastAPI(docs_url=None, redoc_url=None)
app.include_router(
    create_ucp_checkout_router(
        commerce_service=commerce,
        buyer_profile_resolver=resolver,
        protocol_store=store,
        merchant_private_key=merchant_key,
        merchant_key_id=merchant_key_id,
        public_gateway_url=GATEWAY_ORIGIN,
    )
)


@app.get("/ucp/shopping/catalog")
async def catalog() -> dict[str, object]:
    return {
        "items": [
            {
                "id": "prod_stride",
                "name": "Stride Runner",
                "description": "Everyday road running shoe",
                "variants": [_variant()],
            }
        ],
        "next_cursor": None,
    }


@app.get("/ucp/shopping/catalog/variants/{variant_id}")
async def variant(variant_id: str) -> dict[str, object]:
    value = _variant()
    value.update({"product_id": "prod_stride", "product_name": "Stride Runner"})
    return value


@app.post("/test/inventory/{quantity}")
async def set_inventory(quantity: int) -> dict[str, int]:
    global inventory_quantity
    inventory_quantity = quantity
    return {"available_quantity": inventory_quantity}


@app.get("/test/state")
async def state() -> dict[str, object]:
    return {
        "pinned": store.pin is not None,
        "checkout_calls": commerce.calls,
        "exchange_outcomes": [event.outcome for event in store.exchanges],
        "payments_created": False,
    }


def _variant() -> dict[str, object]:
    return {
        "id": VARIANT_ID,
        "sku": "STRIDE-42-BLK",
        "size": "42",
        "color": "Black",
        "unit_price_minor": 249_900,
        "currency": "INR",
        "available_quantity": inventory_quantity,
        "inventory_version": 5,
    }
