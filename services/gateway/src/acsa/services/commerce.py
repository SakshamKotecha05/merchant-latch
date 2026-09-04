"""Application service for authoritative checkout mutations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from acsa.domain.commerce import (
    ApprovalPreview,
    ApprovalResult,
    AuthoritativeCheckout,
    CommerceMutationResult,
    RequestedLine,
)
from acsa.ports.commerce import CommerceStorePort


class CommerceService:
    def __init__(
        self,
        *,
        store: CommerceStorePort,
        merchant_id: str,
        pickup_location_id: str,
        public_merchant_url: str,
        clock: Callable[[], datetime] | None = None,
        checkout_id_factory: Callable[[], str] | None = None,
        continue_token_issuer: Callable[[str, int, datetime], str] | None = None,
    ) -> None:
        self._store = store
        self._merchant_id = merchant_id
        self._pickup_location_id = pickup_location_id
        self._public_merchant_url = public_merchant_url.rstrip("/")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._checkout_id_factory = checkout_id_factory or (lambda: f"chk_{uuid4().hex}")
        self._continue_token_issuer = continue_token_issuer

    async def create_checkout(
        self,
        *,
        buyer_key_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        idempotency_key: str,
        request_sha256: str,
        requested_lines: Sequence[RequestedLine],
        budget_minor: int | None,
    ) -> CommerceMutationResult:
        checkout_id = self._checkout_id_factory()
        now = self._clock()
        continue_url = f"{self._public_merchant_url}/checkout/{checkout_id}"
        if self._continue_token_issuer is not None:
            token = self._continue_token_issuer(checkout_id, 1, now)
            continue_url = f"{continue_url}?version=1&session={token}"
        return await self._store.create_checkout(
            checkout_id=checkout_id,
            merchant_id=self._merchant_id,
            buyer_key_id=buyer_key_id,
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            requested_lines=requested_lines,
            pickup_location_id=self._pickup_location_id,
            budget_minor=budget_minor,
            continue_url=continue_url,
            expires_at=now + timedelta(minutes=30),
        )

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
        budget_minor: int | None,
    ) -> CommerceMutationResult:
        continue_url = f"{self._public_merchant_url}/checkout/{checkout_id}"
        if self._continue_token_issuer is not None:
            version = expected_version + 1
            token = self._continue_token_issuer(checkout_id, version, self._clock())
            continue_url = f"{continue_url}?version={version}&session={token}"
        return await self._store.update_checkout(
            checkout_id=checkout_id,
            buyer_key_id=buyer_key_id,
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            requested_lines=requested_lines,
            pickup_location_id=self._pickup_location_id,
            budget_minor=budget_minor,
            continue_url=continue_url,
        )

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
    ) -> CommerceMutationResult:
        return await self._store.cancel_checkout(
            checkout_id=checkout_id,
            buyer_key_id=buyer_key_id,
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )

    async def get_checkout(
        self, checkout_id: str, *, buyer_key_id: str
    ) -> AuthoritativeCheckout | None:
        return await self._store.get_checkout(checkout_id, buyer_key_id=buyer_key_id)

    async def preview_approval(
        self,
        *,
        checkout_id: str,
        expected_version: int,
        approved_at: datetime,
    ) -> ApprovalPreview:
        return await self._store.preview_approval(
            checkout_id=checkout_id,
            expected_version=expected_version,
            approved_at=approved_at,
        )

    async def approve_checkout(
        self,
        *,
        checkout_id: str,
        expected_version: int,
        snapshot_checksum: str,
        idempotency_key: str,
        request_sha256: str,
        approved_at: datetime,
    ) -> ApprovalResult:
        return await self._store.approve_checkout(
            checkout_id=checkout_id,
            expected_version=expected_version,
            snapshot_checksum=snapshot_checksum,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            approved_at=approved_at,
        )
