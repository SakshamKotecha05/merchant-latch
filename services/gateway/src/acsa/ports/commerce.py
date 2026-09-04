"""Persistence boundary for merchant catalog and commerce state."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from acsa.domain.commerce import (
    ApprovalPreview,
    ApprovalResult,
    AuthoritativeCheckout,
    CatalogPage,
    CatalogVariant,
    CommerceMutationResult,
    RequestedLine,
)


class CommerceStorePort(Protocol):
    async def search_catalog(
        self,
        *,
        query: str | None,
        limit: int,
        after_product_id: str | None,
    ) -> CatalogPage: ...

    async def get_variant(self, variant_id: str) -> CatalogVariant | None: ...

    async def create_checkout(
        self,
        *,
        checkout_id: str,
        merchant_id: str,
        buyer_key_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        idempotency_key: str,
        request_sha256: str,
        requested_lines: Sequence[RequestedLine],
        pickup_location_id: str,
        budget_minor: int | None,
        continue_url: str,
        expires_at: datetime,
    ) -> CommerceMutationResult: ...

    async def update_checkout(
        self,
        *,
        checkout_id: str,
        buyer_key_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        expected_version: int,
        idempotency_key: str,
        request_sha256: str,
        requested_lines: Sequence[RequestedLine],
        pickup_location_id: str,
        budget_minor: int | None,
    ) -> CommerceMutationResult: ...

    async def cancel_checkout(
        self,
        *,
        checkout_id: str,
        buyer_key_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        expected_version: int,
        idempotency_key: str,
        request_sha256: str,
    ) -> CommerceMutationResult: ...

    async def get_checkout(
        self, checkout_id: str, *, buyer_key_id: str
    ) -> AuthoritativeCheckout | None: ...

    async def preview_approval(
        self,
        *,
        checkout_id: str,
        expected_version: int,
        approved_at: datetime,
    ) -> ApprovalPreview: ...

    async def approve_checkout(
        self,
        *,
        checkout_id: str,
        expected_version: int,
        snapshot_checksum: str,
        idempotency_key: str,
        request_sha256: str,
        approved_at: datetime,
    ) -> ApprovalResult: ...
