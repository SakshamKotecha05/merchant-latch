from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from acsa.adapters.postgres.ucp_protocol import TrustPin, UCPExchange

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)
EXCHANGE_ID = UUID("00000000-0000-4000-8000-000000000123")


class MemoryInspectorStore:
    def __init__(self) -> None:
        self.last_exchange_query: tuple[int, tuple[datetime, UUID] | None] | None = None
        self.last_pin_query: tuple[int, str | None] | None = None

    async def list_trust_pins(self, *, limit: int, after: str | None) -> tuple[TrustPin, ...]:
        self.last_pin_query = (limit, after)
        return (
            TrustPin(
                origin="https://buyer.example",
                profile_url="https://buyer.example/private-path?secret=sentinel",
                key_id="buyer-p256-2026-01",
                fingerprint="a" * 64,
                version="2026-04-08",
                first_seen_at=NOW,
                last_seen_at=NOW,
            ),
        )

    async def list_exchanges(
        self,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> tuple[UCPExchange, ...]:
        self.last_exchange_query = (limit, before)
        return (self._exchange(),)

    async def get_exchange(self, exchange_id: UUID) -> UCPExchange | None:
        return self._exchange() if exchange_id == EXCHANGE_ID else None

    def _exchange(self) -> UCPExchange:
        return UCPExchange(
            id=EXCHANGE_ID,
            method="POST",
            route="/ucp/shopping/checkout-sessions",
            profile_origin="https://buyer.example",
            profile_url_sha256="b" * 64,
            buyer_key_id="buyer-p256-2026-01",
            buyer_fingerprint="a" * 64,
            nonce_sha256="c" * 64,
            request_sha256="d" * 64,
            response_sha256="e" * 64,
            http_status=201,
            outcome="accepted",
            checkout_id="chk_1",
            started_at=NOW,
            completed_at=NOW,
        )


def _client(store: MemoryInspectorStore | None = None) -> TestClient:
    assert importlib.util.find_spec("acsa.web.ucp_inspector") is not None
    from acsa.web.ucp_inspector import create_ucp_inspector_router

    app = FastAPI()
    app.include_router(
        create_ucp_inspector_router(
            store=store or MemoryInspectorStore(),
            inspector_token="x" * 32,
        )
    )
    return TestClient(app)


def test_rejects_missing_and_wrong_tokens_with_the_same_safe_response() -> None:
    client = _client()

    missing = client.get("/internal/ucp/trust-pins")
    wrong = client.get(
        "/internal/ucp/trust-pins",
        headers={"Authorization": "Bearer " + "y" * 32},
    )

    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json() == {"code": "authentication_failed"}
    assert missing.headers["cache-control"] == wrong.headers["cache-control"] == "no-store"


def test_lists_only_redacted_trust_and_exchange_fields() -> None:
    client = _client()
    headers = {"Authorization": "Bearer " + "x" * 32}

    pins = client.get("/internal/ucp/trust-pins", headers=headers)
    exchanges = client.get("/internal/ucp/exchanges", headers=headers)
    detail = client.get(f"/internal/ucp/exchanges/{EXCHANGE_ID}", headers=headers)

    assert pins.status_code == exchanges.status_code == detail.status_code == 200
    combined = pins.text + exchanges.text + detail.text
    assert "private-path" not in combined
    assert "sentinel" not in combined
    assert "signature" not in combined
    assert "authorization" not in combined
    assert pins.json()["items"][0]["origin"] == "https://buyer.example"
    assert exchanges.json()["items"][0]["id"] == str(EXCHANGE_ID)
    assert detail.json()["outcome"] == "accepted"
    assert all(
        response.headers["cache-control"] == "no-store" for response in (pins, exchanges, detail)
    )


def test_returns_safe_not_found_and_validates_page_limit() -> None:
    client = _client()
    headers = {"Authorization": "Bearer " + "x" * 32}

    missing = client.get(
        "/internal/ucp/exchanges/00000000-0000-4000-8000-000000000999",
        headers=headers,
    )
    invalid_limit = client.get("/internal/ucp/exchanges?limit=101", headers=headers)

    assert missing.status_code == 404
    assert missing.json() == {"code": "exchange_not_found"}
    assert invalid_limit.status_code == 400
    assert invalid_limit.headers["cache-control"] == "no-store"
    assert "x" * 32 not in invalid_limit.text


def test_invalid_exchange_identifiers_are_authenticated_and_never_cacheable() -> None:
    client = _client()
    headers = {"Authorization": "Bearer " + "x" * 32}

    unauthorized = client.get("/internal/ucp/exchanges/not-a-uuid")
    invalid = client.get("/internal/ucp/exchanges/not-a-uuid", headers=headers)

    assert unauthorized.status_code == 401
    assert unauthorized.json() == {"code": "authentication_failed"}
    assert invalid.status_code == 400
    assert invalid.json() == {"code": "invalid_exchange_id"}
    assert unauthorized.headers["cache-control"] == invalid.headers["cache-control"] == "no-store"


def test_exchange_pagination_uses_the_timestamp_and_id_as_one_stable_cursor() -> None:
    store = MemoryInspectorStore()
    client = _client(store)
    headers = {"Authorization": "Bearer " + "x" * 32}

    response = client.get(
        "/internal/ucp/exchanges",
        params={"limit": "1", "before": NOW.isoformat(), "before_id": str(EXCHANGE_ID)},
        headers=headers,
    )

    assert response.status_code == 200
    assert store.last_exchange_query == (1, (NOW, EXCHANGE_ID))
    assert response.json()["next"] == {
        "before": NOW.isoformat(),
        "before_id": str(EXCHANGE_ID),
    }


def test_exchange_pagination_rejects_a_partial_cursor_without_leaking_cache_data() -> None:
    client = _client()
    headers = {"Authorization": "Bearer " + "x" * 32}

    response = client.get(
        f"/internal/ucp/exchanges?before_id={EXCHANGE_ID}",
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_pagination"}
    assert response.headers["cache-control"] == "no-store"


def test_trust_pin_listing_is_bounded_and_paginates_by_origin() -> None:
    store = MemoryInspectorStore()
    client = _client(store)
    headers = {"Authorization": "Bearer " + "x" * 32}

    response = client.get(
        "/internal/ucp/trust-pins",
        params={"limit": "1", "after": "https://another-buyer.example"},
        headers=headers,
    )

    assert response.status_code == 200
    assert store.last_pin_query == (1, "https://another-buyer.example")
    assert response.json()["next"] == {"after": "https://buyer.example"}
