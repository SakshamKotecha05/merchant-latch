"""Public merchant catalog search and exact variant lookup."""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from acsa.domain.commerce import CatalogItem, CatalogVariant
from acsa.ports.commerce import CommerceStorePort


def create_catalog_router(store: CommerceStorePort) -> APIRouter:
    router = APIRouter()

    @router.get("/ucp/shopping/catalog")
    async def search_catalog(
        q: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=20, ge=1, le=50),
        cursor: str | None = Query(default=None, max_length=128),
    ) -> JSONResponse:
        try:
            after_product_id = _decode_cursor(cursor) if cursor is not None else None
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"code": "invalid_cursor", "content": "The catalog cursor is invalid."},
            )
        page = await store.search_catalog(
            query=q,
            limit=limit,
            after_product_id=after_product_id,
        )
        return JSONResponse(
            {
                "items": [_item_resource(item) for item in page.items],
                "next_cursor": (
                    _encode_cursor(page.next_product_id)
                    if page.next_product_id is not None
                    else None
                ),
            }
        )

    @router.get("/ucp/shopping/catalog/variants/{variant_id}")
    async def get_variant(variant_id: str) -> JSONResponse:
        variant = await store.get_variant(variant_id)
        if variant is None:
            return JSONResponse(
                status_code=404,
                content={
                    "code": "variant_not_found",
                    "content": "The catalog variant does not exist.",
                },
            )
        return JSONResponse(_variant_resource(variant, include_product=True))

    return router


def _item_resource(item: CatalogItem) -> dict[str, object]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "variants": [
            _variant_resource(variant, include_product=False) for variant in item.variants
        ],
    }


def _variant_resource(variant: CatalogVariant, *, include_product: bool) -> dict[str, object]:
    resource: dict[str, object] = {
        "id": variant.id,
        "sku": variant.sku,
        "size": variant.size,
        "color": variant.color,
        "unit_price_minor": variant.unit_price_minor,
        "currency": variant.currency,
        "available_quantity": variant.available_quantity,
        "inventory_version": variant.inventory_version,
    }
    if include_product:
        resource["product_id"] = variant.product_id
        resource["product_name"] = variant.product_name
    return resource


def _encode_cursor(product_id: str) -> str:
    return base64.urlsafe_b64encode(product_id.encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> str:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = base64.b64decode(cursor + padding, altchars=b"-_", validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("invalid catalog cursor") from None
    if not value or len(value) > 64:
        raise ValueError("invalid catalog cursor")
    return value
