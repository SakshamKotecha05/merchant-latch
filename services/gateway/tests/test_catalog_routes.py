from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from acsa.domain.commerce import CatalogItem, CatalogPage, CatalogVariant
from acsa.web.catalog import create_catalog_router


class CatalogStore:
    def __init__(self) -> None:
        self.variant = CatalogVariant(
            id="var_stride_42_black",
            product_id="prod_stride",
            product_name="Stride One",
            sku="ML-STRIDE-BLK-42",
            size="42",
            color="Black",
            unit_price_minor=499_900,
            currency="INR",
            available_quantity=5,
            inventory_version=3,
        )

    async def search_catalog(
        self, *, query: str | None, limit: int, after_product_id: str | None
    ) -> CatalogPage:
        if query == "empty":
            return CatalogPage(items=(), next_product_id=None)
        assert limit <= 50
        return CatalogPage(
            items=(
                CatalogItem(
                    id="prod_stride",
                    name="Stride One",
                    description="A clean everyday sneaker.",
                    variants=(self.variant,),
                ),
            ),
            next_product_id="prod_stride" if after_product_id is None else None,
        )

    async def get_variant(self, variant_id: str) -> CatalogVariant | None:
        return self.variant if variant_id == self.variant.id else None


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_catalog_router(CatalogStore()))
    return TestClient(app)


def test_catalog_search_returns_only_public_merchant_fields() -> None:
    response = _client().get("/ucp/shopping/catalog", params={"q": "stride", "limit": 10})

    assert response.status_code == 200
    assert response.json()["items"][0] == {
        "id": "prod_stride",
        "name": "Stride One",
        "description": "A clean everyday sneaker.",
        "variants": [
            {
                "id": "var_stride_42_black",
                "sku": "ML-STRIDE-BLK-42",
                "size": "42",
                "color": "Black",
                "unit_price_minor": 499_900,
                "currency": "INR",
                "available_quantity": 5,
                "inventory_version": 3,
            }
        ],
    }
    assert response.json()["next_cursor"] == "cHJvZF9zdHJpZGU"
    assert "on_hand" not in response.text
    assert "reserved" not in response.text
    assert "sold" not in response.text


def test_exact_variant_lookup_returns_authoritative_terms() -> None:
    response = _client().get("/ucp/shopping/catalog/variants/var_stride_42_black")

    assert response.status_code == 200
    assert response.json()["product_name"] == "Stride One"
    assert response.json()["unit_price_minor"] == 499_900
    assert response.json()["available_quantity"] == 5


def test_exact_variant_lookup_rejects_unknown_identifier() -> None:
    response = _client().get("/ucp/shopping/catalog/variants/var_missing")

    assert response.status_code == 404
    assert response.json() == {
        "code": "variant_not_found",
        "content": "The catalog variant does not exist.",
    }


def test_catalog_rejects_invalid_cursor_without_database_access() -> None:
    response = _client().get("/ucp/shopping/catalog", params={"cursor": "%%%"})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


def test_catalog_bounds_page_size() -> None:
    assert _client().get("/ucp/shopping/catalog", params={"limit": 0}).status_code == 422
    assert _client().get("/ucp/shopping/catalog", params={"limit": 51}).status_code == 422
