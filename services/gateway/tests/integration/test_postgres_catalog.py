from __future__ import annotations

import pytest

from acsa.adapters.postgres.commerce import PostgresCommerceStore
from acsa.adapters.postgres.models import Inventory, MerchantConfig, Product, Variant

pytestmark = pytest.mark.integration


async def _seed(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session, session.begin():
        session.add(
            MerchantConfig(
                id="merchant_demo",
                public_name="MerchantLatch",
                currency="INR",
                active_policy_pack_version=1,
            )
        )
        session.add_all(
            [
                Product(
                    id="prod_court",
                    merchant_id="merchant_demo",
                    name="Court Low",
                    description="A low-profile court sneaker.",
                    search_text="court low profile sneaker",
                    active=True,
                ),
                Product(
                    id="prod_stride",
                    merchant_id="merchant_demo",
                    name="Stride One",
                    description="A clean everyday sneaker.",
                    search_text="stride one clean everyday sneaker",
                    active=True,
                ),
                Product(
                    id="prod_hidden",
                    merchant_id="merchant_demo",
                    name="Hidden",
                    description="Inactive product.",
                    search_text="hidden inactive product",
                    active=False,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Variant(
                    id="var_court_41_stone",
                    product_id="prod_court",
                    sku="ML-COURT-STN-41",
                    size="41",
                    color="Stone",
                    unit_price_minor=549_900,
                    currency="INR",
                    active=True,
                ),
                Variant(
                    id="var_stride_42_black",
                    product_id="prod_stride",
                    sku="ML-STRIDE-BLK-42",
                    size="42",
                    color="Black",
                    unit_price_minor=499_900,
                    currency="INR",
                    active=True,
                ),
                Variant(
                    id="var_stride_43_hidden",
                    product_id="prod_stride",
                    sku="ML-STRIDE-HDN-43",
                    size="43",
                    color="Hidden",
                    unit_price_minor=499_900,
                    currency="INR",
                    active=False,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Inventory(
                    variant_id="var_court_41_stone",
                    on_hand=8,
                    reserved=1,
                    sold=2,
                    version=4,
                ),
                Inventory(
                    variant_id="var_stride_42_black",
                    on_hand=5,
                    reserved=1,
                    sold=0,
                    version=3,
                ),
                Inventory(
                    variant_id="var_stride_43_hidden",
                    on_hand=5,
                    reserved=0,
                    sold=0,
                    version=1,
                ),
            ]
        )


async def test_search_is_case_insensitive_and_excludes_inactive_records(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)

    page = await store.search_catalog(query="  STRIDE  ", limit=10, after_product_id=None)

    assert [item.id for item in page.items] == ["prod_stride"]
    assert [variant.id for variant in page.items[0].variants] == ["var_stride_42_black"]
    assert page.items[0].variants[0].available_quantity == 4


async def test_catalog_pagination_is_stable(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)

    first = await store.search_catalog(query=None, limit=1, after_product_id=None)
    second = await store.search_catalog(query=None, limit=1, after_product_id=first.next_product_id)

    assert [item.id for item in first.items] == ["prod_court"]
    assert first.next_product_id == "prod_court"
    assert [item.id for item in second.items] == ["prod_stride"]
    assert second.next_product_id is None


async def test_exact_variant_lookup_uses_inventory_counters(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(session_factory)
    store = PostgresCommerceStore(session_factory)

    variant = await store.get_variant("var_court_41_stone")

    assert variant is not None
    assert variant.sku == "ML-COURT-STN-41"
    assert variant.available_quantity == 5
    assert variant.inventory_version == 4
    assert await store.get_variant("var_stride_43_hidden") is None
