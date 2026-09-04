"""PostgreSQL catalog and commerce persistence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import orjson
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from acsa.adapters.postgres.models import (
    ApprovalSnapshotRecord,
    AuditEvent,
    CheckoutLine,
    CheckoutSession,
    CommerceIdempotencyRecord,
    Inventory,
    InventoryLease,
    MerchantConfig,
    MerchantOrder,
    OutboxJob,
    PaymentAttempt,
    PickupLocation,
    PolicyPack,
    Product,
    UCPCheckout,
    UCPRequestNonce,
    Variant,
)
from acsa.domain.canonical import canonical_json_bytes
from acsa.domain.commerce import (
    ApprovalOutcome,
    ApprovalPreview,
    ApprovalResult,
    ApprovalSnapshot,
    AuthoritativeCheckout,
    CatalogItem,
    CatalogPage,
    CatalogVariant,
    CheckoutPricing,
    CheckoutStatus,
    CommerceMutationOutcome,
    CommerceMutationResult,
    CommerceRuleViolation,
    InventoryLeaseState,
    PaymentAttemptState,
    PolicyRules,
    PricedLine,
    RequestedLine,
    VariantTerms,
    build_approval_snapshot,
    price_checkout,
)
from acsa.domain.receipts import build_receipt
from acsa.domain.ucp_checkout import UCP_VERSION


class PostgresCommerceStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def search_catalog(
        self,
        *,
        query: str | None,
        limit: int,
        after_product_id: str | None,
    ) -> CatalogPage:
        statement = select(Product).where(Product.active.is_(True))
        normalized_query = " ".join(query.casefold().split()) if query else ""
        if normalized_query:
            pattern = f"%{_escape_like(normalized_query)}%"
            statement = statement.where(Product.search_text.ilike(pattern, escape="\\"))
        if after_product_id is not None:
            statement = statement.where(Product.id > after_product_id)
        async with self._session_factory() as session:
            products = list(await session.scalars(statement.order_by(Product.id).limit(limit + 1)))
            has_more = len(products) > limit
            products = products[:limit]
            if not products:
                return CatalogPage(items=(), next_product_id=None)
            variants = await _variants_for_products(session, [product.id for product in products])

        grouped: defaultdict[str, list[CatalogVariant]] = defaultdict(list)
        for variant in variants:
            grouped[variant.product_id].append(variant)
        items = tuple(
            CatalogItem(
                id=product.id,
                name=product.name,
                description=product.description,
                variants=tuple(grouped[product.id]),
            )
            for product in products
        )
        return CatalogPage(
            items=items,
            next_product_id=products[-1].id if has_more else None,
        )

    async def get_variant(self, variant_id: str) -> CatalogVariant | None:
        statement = (
            select(Variant, Inventory, Product)
            .join(Inventory, Inventory.variant_id == Variant.id)
            .join(Product, Product.id == Variant.product_id)
            .where(
                Variant.id == variant_id,
                Variant.active.is_(True),
                Product.active.is_(True),
            )
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        return _to_catalog_variant(*row) if row is not None else None

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
    ) -> CommerceMutationResult:
        async with self._session_factory() as session, session.begin():
            replay = await _idempotency_result(
                session,
                buyer_key_id=buyer_key_id,
                operation="create_checkout",
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                return replay
            await _lock(session, f"nonce:{buyer_key_id}:{nonce}")
            nonce_exists = await session.scalar(
                select(UCPRequestNonce.checkout_id).where(
                    UCPRequestNonce.buyer_key_id == buyer_key_id,
                    UCPRequestNonce.nonce == nonce,
                )
            )
            if nonce_exists is not None:
                return CommerceMutationResult(
                    CommerceMutationOutcome.CONFLICT,
                    rule_ids=("nonce_replayed",),
                )
            merchant = await session.get(MerchantConfig, merchant_id)
            if merchant is None:
                return _blocked("merchant_not_found")
            pickup = await session.get(PickupLocation, pickup_location_id)
            if pickup is None or not pickup.active or pickup.merchant_id != merchant_id:
                return _blocked("pickup_location_invalid")
            try:
                rules = await _policy_rules(session, merchant)
                lines, pricing = await _authoritative_price(
                    session, requested_lines, rules, budget_minor
                )
            except CommerceRuleViolation as error:
                return _blocked(error.rule_id)

            record = CheckoutSession(
                id=checkout_id,
                merchant_id=merchant_id,
                buyer_key_id=buyer_key_id,
                status=CheckoutStatus.REQUIRES_BUYER_REVIEW.value,
                version=1,
                policy_pack_version=merchant.active_policy_pack_version,
                pickup_location_id=pickup_location_id,
                currency="INR",
                budget_minor=budget_minor,
                expires_at=expires_at,
            )
            session.add(record)
            await session.flush()
            _replace_lines(session, record.id, lines)
            checkout = _checkout_value(
                record,
                lines,
                continue_url=continue_url,
                pricing=pricing,
            )
            session.add(
                UCPCheckout(
                    id=record.id,
                    buyer_key_id=buyer_key_id,
                    status="requires_escalation",
                    continue_url=continue_url,
                    expires_at=expires_at,
                    resource=_resource_from_bytes(checkout.canonical_bytes),
                    response_body=checkout.canonical_bytes,
                )
            )
            session.add(
                UCPRequestNonce(
                    buyer_key_id=buyer_key_id,
                    nonce=nonce,
                    expires_at=nonce_expires_at,
                    checkout_id=record.id,
                )
            )
            session.add(
                CommerceIdempotencyRecord(
                    buyer_key_id=buyer_key_id,
                    operation="create_checkout",
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    checkout_id=record.id,
                    response_body=checkout.canonical_bytes,
                )
            )
            session.add(
                AuditEvent(
                    aggregate_type="checkout",
                    aggregate_id=record.id,
                    sequence=1,
                    event_type="checkout.created",
                    payload={
                        "checkout_version": 1,
                        "policy_pack_version": record.policy_pack_version,
                    },
                    evidence_source="signed_ucp_request",
                )
            )
            return CommerceMutationResult(
                CommerceMutationOutcome.CREATED,
                checkout=checkout,
                response_body=checkout.canonical_bytes,
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
        pickup_location_id: str,
        budget_minor: int | None,
        continue_url: str | None = None,
    ) -> CommerceMutationResult:
        async with self._session_factory() as session, session.begin():
            replay = await _idempotency_result(
                session,
                buyer_key_id=buyer_key_id,
                operation=f"update_checkout:{checkout_id}",
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                return replay
            nonce_conflict = await _nonce_conflict(session, buyer_key_id, nonce)
            if nonce_conflict is not None:
                return nonce_conflict
            record = await session.scalar(
                select(CheckoutSession).where(CheckoutSession.id == checkout_id).with_for_update()
            )
            if record is None or record.buyer_key_id != buyer_key_id:
                return CommerceMutationResult(CommerceMutationOutcome.NOT_FOUND)
            if record.version != expected_version:
                return CommerceMutationResult(CommerceMutationOutcome.STALE)
            if record.status not in {
                CheckoutStatus.OPEN.value,
                CheckoutStatus.REQUIRES_BUYER_REVIEW.value,
            }:
                return _blocked("checkout_immutable")
            pickup = await session.get(PickupLocation, pickup_location_id)
            if pickup is None or not pickup.active or pickup.merchant_id != record.merchant_id:
                return _blocked("pickup_location_invalid")
            merchant = await session.get(MerchantConfig, record.merchant_id)
            if merchant is None:
                return _blocked("merchant_not_found")
            try:
                rules = await _policy_rules(session, merchant)
                lines, pricing = await _authoritative_price(
                    session, requested_lines, rules, budget_minor
                )
            except CommerceRuleViolation as error:
                return _blocked(error.rule_id)

            projection = await session.get(UCPCheckout, record.id)
            if projection is None:
                return _blocked("checkout_projection_missing")
            await session.execute(delete(CheckoutLine).where(CheckoutLine.checkout_id == record.id))
            record.version += 1
            if continue_url is not None:
                projection.continue_url = continue_url
            record.policy_pack_version = merchant.active_policy_pack_version
            record.pickup_location_id = pickup_location_id
            record.budget_minor = budget_minor
            record.status = CheckoutStatus.REQUIRES_BUYER_REVIEW.value
            _replace_lines(session, record.id, lines)
            checkout = _checkout_value(
                record,
                lines,
                continue_url=projection.continue_url,
                pricing=pricing,
            )
            session.add(
                CommerceIdempotencyRecord(
                    buyer_key_id=buyer_key_id,
                    operation=f"update_checkout:{checkout_id}",
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    checkout_id=record.id,
                    response_body=checkout.canonical_bytes,
                )
            )
            session.add(
                UCPRequestNonce(
                    buyer_key_id=buyer_key_id,
                    nonce=nonce,
                    expires_at=nonce_expires_at,
                    checkout_id=record.id,
                )
            )
            await _append_audit(
                session,
                record.id,
                "checkout.updated",
                {
                    "checkout_version": record.version,
                    "policy_pack_version": record.policy_pack_version,
                },
            )
            return CommerceMutationResult(
                CommerceMutationOutcome.UPDATED,
                checkout=checkout,
                response_body=checkout.canonical_bytes,
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
        operation = f"cancel_checkout:{checkout_id}"
        async with self._session_factory() as session, session.begin():
            replay = await _idempotency_result(
                session,
                buyer_key_id=buyer_key_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                return replay
            nonce_conflict = await _nonce_conflict(session, buyer_key_id, nonce)
            if nonce_conflict is not None:
                return nonce_conflict
            record = await session.scalar(
                select(CheckoutSession).where(CheckoutSession.id == checkout_id).with_for_update()
            )
            if record is None or record.buyer_key_id != buyer_key_id:
                return CommerceMutationResult(CommerceMutationOutcome.NOT_FOUND)
            if record.version != expected_version:
                return CommerceMutationResult(CommerceMutationOutcome.STALE)
            if record.status not in {
                CheckoutStatus.OPEN.value,
                CheckoutStatus.REQUIRES_BUYER_REVIEW.value,
                CheckoutStatus.APPROVED.value,
            }:
                return _blocked("checkout_cannot_cancel")
            record.status = CheckoutStatus.CANCELED.value
            record.version += 1
            checkout = await _load_checkout(session, record)
            if checkout is None:
                return _blocked("checkout_lines_missing")
            session.add(
                CommerceIdempotencyRecord(
                    buyer_key_id=buyer_key_id,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    checkout_id=record.id,
                    response_body=checkout.canonical_bytes,
                )
            )
            session.add(
                UCPRequestNonce(
                    buyer_key_id=buyer_key_id,
                    nonce=nonce,
                    expires_at=nonce_expires_at,
                    checkout_id=record.id,
                )
            )
            await _append_audit(
                session,
                record.id,
                "checkout.canceled",
                {"checkout_version": record.version},
            )
            return CommerceMutationResult(
                CommerceMutationOutcome.CANCELED,
                checkout=checkout,
                response_body=checkout.canonical_bytes,
            )

    async def preview_approval(
        self,
        *,
        checkout_id: str,
        expected_version: int,
        approved_at: datetime,
    ) -> ApprovalPreview:
        async with self._session_factory() as session:
            record = await session.get(CheckoutSession, checkout_id)
            if record is None:
                return ApprovalPreview(ApprovalOutcome.NOT_FOUND)
            if record.version != expected_version:
                return ApprovalPreview(ApprovalOutcome.STALE)
            try:
                context = await _approval_context(
                    session,
                    record,
                    approved_at=approved_at,
                    lock_inventory=False,
                )
            except CommerceRuleViolation as error:
                return ApprovalPreview(ApprovalOutcome.BLOCKED, rule_ids=(error.rule_id,))
            return ApprovalPreview(ApprovalOutcome.READY, snapshot=context.snapshot)

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
        operation = f"approve_checkout:{checkout_id}"
        async with self._session_factory() as session, session.begin():
            replay = await _approval_idempotency_result(
                session,
                operation=operation,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                return replay
            record = await session.scalar(
                select(CheckoutSession).where(CheckoutSession.id == checkout_id).with_for_update()
            )
            if record is None:
                return ApprovalResult(ApprovalOutcome.NOT_FOUND)
            if record.version != expected_version:
                return ApprovalResult(ApprovalOutcome.STALE)
            try:
                context = await _approval_context(
                    session,
                    record,
                    approved_at=approved_at,
                    lock_inventory=True,
                )
            except CommerceRuleViolation as error:
                return ApprovalResult(ApprovalOutcome.BLOCKED, rule_ids=(error.rule_id,))
            if context.snapshot.checksum != snapshot_checksum:
                return ApprovalResult(
                    ApprovalOutcome.BLOCKED,
                    rule_ids=("snapshot_checksum_changed",),
                )

            for line in context.lines:
                inventory = context.inventory[line.variant_id]
                inventory.reserved += line.quantity
                inventory.version += 1
            snapshot_record = ApprovalSnapshotRecord(
                checkout_id=record.id,
                checkout_version=record.version,
                policy_pack_version=record.policy_pack_version,
                checksum=context.snapshot.checksum,
                canonical_body=context.snapshot.canonical_bytes,
                approved_by=record.buyer_key_id,
                approved_at=approved_at,
                expires_at=context.snapshot.expires_at,
            )
            session.add(snapshot_record)
            await session.flush()
            attempt_version = (
                int(
                    await session.scalar(
                        select(func.coalesce(func.max(PaymentAttempt.attempt_version), 0)).where(
                            PaymentAttempt.checkout_id == record.id
                        )
                    )
                    or 0
                )
                + 1
            )
            attempt_id = f"att_{uuid4().hex}"
            receipt = build_receipt(
                merchant_id=record.merchant_id,
                payment_attempt_id=attempt_id,
                snapshot_checksum=context.snapshot.checksum,
            )
            attempt = PaymentAttempt(
                id=attempt_id,
                checkout_id=record.id,
                attempt_version=attempt_version,
                state=PaymentAttemptState.DRAFT.value,
                receipt=receipt,
                snapshot_id=snapshot_record.id,
                snapshot_checksum=context.snapshot.checksum,
                amount_minor=context.pricing.total_minor,
                currency=context.pricing.currency,
                provider_uncertain=False,
            )
            session.add(attempt)
            await session.flush()
            lease_expires_at = min(
                context.snapshot.expires_at,
                approved_at + context.rules.inventory_lease_lifetime,
            )
            session.add(
                InventoryLease(
                    attempt_id=attempt_id,
                    state=InventoryLeaseState.ACTIVE.value,
                    expires_at=lease_expires_at,
                )
            )
            record.status = CheckoutStatus.APPROVED.value
            record.version += 1
            response_body = canonical_json_bytes(
                {
                    "checkout_id": record.id,
                    "checkout_version": record.version,
                    "status": record.status,
                    "attempt": {"id": attempt_id, "state": PaymentAttemptState.DRAFT.value},
                    "snapshot_checksum": context.snapshot.checksum,
                    "lease_expires_at": lease_expires_at.isoformat().replace("+00:00", "Z"),
                }
            )
            session.add(
                CommerceIdempotencyRecord(
                    buyer_key_id=record.buyer_key_id,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    checkout_id=record.id,
                    response_body=response_body,
                )
            )
            session.add(
                OutboxJob(
                    job_type="create_provider_order",
                    aggregate_type="payment_attempt",
                    aggregate_id=attempt_id,
                    payload={
                        "attempt_id": attempt_id,
                        "checkout_id": record.id,
                        "snapshot_checksum": context.snapshot.checksum,
                    },
                    max_attempts=6,
                )
            )
            await _append_audit(
                session,
                record.id,
                "checkout.approved",
                {
                    "checkout_version": record.version,
                    "attempt_id": attempt_id,
                    "snapshot_checksum": context.snapshot.checksum,
                },
                evidence_source="human_browser",
            )
            return ApprovalResult(
                ApprovalOutcome.APPROVED,
                attempt_id=attempt_id,
                response_body=response_body,
            )

    async def get_checkout(
        self, checkout_id: str, *, buyer_key_id: str
    ) -> AuthoritativeCheckout | None:
        async with self._session_factory() as session:
            record = await session.get(CheckoutSession, checkout_id)
            if record is None or record.buyer_key_id != buyer_key_id:
                return None
            return await _load_checkout(session, record)


@dataclass(frozen=True, slots=True)
class _ApprovalContext:
    lines: tuple[PricedLine, ...]
    pricing: CheckoutPricing
    inventory: dict[str, Inventory]
    rules: PolicyRules
    snapshot: ApprovalSnapshot


async def _approval_context(
    session: AsyncSession,
    record: CheckoutSession,
    *,
    approved_at: datetime,
    lock_inventory: bool,
) -> _ApprovalContext:
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise CommerceRuleViolation("approval_time_invalid")
    if record.status != CheckoutStatus.REQUIRES_BUYER_REVIEW.value:
        raise CommerceRuleViolation("checkout_not_approvable")
    if record.expires_at <= approved_at:
        raise CommerceRuleViolation("checkout_expired")
    merchant = await session.get(MerchantConfig, record.merchant_id)
    if merchant is None:
        raise CommerceRuleViolation("merchant_not_found")
    if merchant.active_policy_pack_version != record.policy_pack_version:
        raise CommerceRuleViolation("policy_version_changed")
    pickup = await session.get(PickupLocation, record.pickup_location_id)
    if pickup is None or not pickup.active or pickup.merchant_id != record.merchant_id:
        raise CommerceRuleViolation("pickup_location_changed")
    rules = await _policy_rules(session, merchant)
    stored_lines = list(
        await session.scalars(
            select(CheckoutLine)
            .where(CheckoutLine.checkout_id == record.id)
            .order_by(CheckoutLine.variant_id)
        )
    )
    if not stored_lines:
        raise CommerceRuleViolation("checkout_lines_missing")
    variant_ids = [line.variant_id for line in stored_lines]
    inventory_statement = (
        select(Inventory)
        .where(Inventory.variant_id.in_(variant_ids))
        .order_by(Inventory.variant_id)
    )
    if lock_inventory:
        inventory_statement = inventory_statement.with_for_update()
    inventory_records = list(await session.scalars(inventory_statement))
    inventory = {item.variant_id: item for item in inventory_records}
    variant_rows = await session.execute(
        select(Variant, Product)
        .join(Product, Product.id == Variant.product_id)
        .where(Variant.id.in_(variant_ids))
        .order_by(Variant.id)
    )
    variants: dict[str, VariantTerms] = {}
    for variant, product in variant_rows:
        stock = inventory.get(variant.id)
        if stock is None:
            raise CommerceRuleViolation("inventory_missing")
        variants[variant.id] = VariantTerms(
            id=variant.id,
            sku=variant.sku,
            product_name=product.name,
            size=variant.size,
            color=variant.color,
            unit_price_minor=variant.unit_price_minor,
            inventory_version=stock.version,
            available_quantity=stock.on_hand - stock.reserved - stock.sold,
            active=variant.active and product.active,
            currency=variant.currency,
        )
    current_lines, pricing = price_checkout(
        requests=[RequestedLine(line.variant_id, line.quantity) for line in stored_lines],
        variants=variants,
        rules=rules,
        budget_minor=record.budget_minor,
    )
    current = {line.variant_id: line for line in current_lines}
    for stored in stored_lines:
        refreshed = current[stored.variant_id]
        if refreshed.inventory_version != stored.inventory_version:
            raise CommerceRuleViolation("inventory_version_changed")
        if refreshed.unit_price_minor != stored.unit_price_minor:
            raise CommerceRuleViolation("price_changed")
        if (
            refreshed.sku != stored.sku
            or refreshed.product_name != stored.product_name
            or refreshed.size != stored.size
            or refreshed.color != stored.color
        ):
            raise CommerceRuleViolation("catalog_terms_changed")
    expires_at = min(record.expires_at, approved_at + rules.approval_lifetime)
    snapshot = build_approval_snapshot(
        merchant_id=record.merchant_id,
        checkout_id=record.id,
        checkout_version=record.version,
        policy_pack_version=record.policy_pack_version,
        lines=current_lines,
        pricing=pricing,
        pickup_location_id=record.pickup_location_id,
        approved_by=record.buyer_key_id,
        approved_at=approved_at,
        expires_at=expires_at,
    )
    return _ApprovalContext(
        lines=tuple(current_lines),
        pricing=pricing,
        inventory=inventory,
        rules=rules,
        snapshot=snapshot,
    )


async def _variants_for_products(
    session: AsyncSession, product_ids: list[str]
) -> list[CatalogVariant]:
    rows = await session.execute(
        select(Variant, Inventory, Product)
        .join(Inventory, Inventory.variant_id == Variant.id)
        .join(Product, Product.id == Variant.product_id)
        .where(Variant.product_id.in_(product_ids), Variant.active.is_(True))
        .order_by(Variant.product_id, Variant.id)
    )
    return [_to_catalog_variant(*row) for row in rows]


def _to_catalog_variant(variant: Variant, inventory: Inventory, product: Product) -> CatalogVariant:
    return CatalogVariant(
        id=variant.id,
        product_id=product.id,
        product_name=product.name,
        sku=variant.sku,
        size=variant.size,
        color=variant.color,
        unit_price_minor=variant.unit_price_minor,
        currency=variant.currency,
        available_quantity=inventory.on_hand - inventory.reserved - inventory.sold,
        inventory_version=inventory.version,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _idempotency_result(
    session: AsyncSession,
    *,
    buyer_key_id: str,
    operation: str,
    idempotency_key: str,
    request_sha256: str,
) -> CommerceMutationResult | None:
    lock_value = f"commerce-idempotency:{buyer_key_id}:{operation}:{idempotency_key}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_value, 0))))
    existing = await session.get(
        CommerceIdempotencyRecord,
        (buyer_key_id, operation, idempotency_key),
    )
    if existing is None:
        return None
    if existing.request_sha256 != request_sha256:
        return CommerceMutationResult(CommerceMutationOutcome.CONFLICT)
    checkout = None
    if existing.checkout_id is not None:
        record = await session.get(CheckoutSession, existing.checkout_id)
        if record is not None:
            checkout = await _load_checkout(session, record)
    return CommerceMutationResult(
        CommerceMutationOutcome.REPLAYED,
        checkout=checkout,
        response_body=bytes(existing.response_body),
    )


async def _approval_idempotency_result(
    session: AsyncSession,
    *,
    operation: str,
    idempotency_key: str,
    request_sha256: str,
) -> ApprovalResult | None:
    await _lock(session, f"approval-idempotency:{operation}:{idempotency_key}")
    existing = await session.scalar(
        select(CommerceIdempotencyRecord).where(
            CommerceIdempotencyRecord.operation == operation,
            CommerceIdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if existing is None:
        return None
    if existing.request_sha256 != request_sha256:
        return ApprovalResult(ApprovalOutcome.CONFLICT)
    response_body = bytes(existing.response_body)
    payload = orjson.loads(response_body)
    attempt_id = payload.get("attempt", {}).get("id") if isinstance(payload, dict) else None
    return ApprovalResult(
        ApprovalOutcome.REPLAYED,
        attempt_id=attempt_id if isinstance(attempt_id, str) else None,
        response_body=response_body,
    )


async def _lock(session: AsyncSession, value: str) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(value, 0))))


async def _nonce_conflict(
    session: AsyncSession, buyer_key_id: str, nonce: str
) -> CommerceMutationResult | None:
    await _lock(session, f"nonce:{buyer_key_id}:{nonce}")
    existing = await session.scalar(
        select(UCPRequestNonce.checkout_id).where(
            UCPRequestNonce.buyer_key_id == buyer_key_id,
            UCPRequestNonce.nonce == nonce,
        )
    )
    if existing is None:
        return None
    return CommerceMutationResult(
        CommerceMutationOutcome.CONFLICT,
        rule_ids=("nonce_replayed",),
    )


async def _policy_rules(session: AsyncSession, merchant: MerchantConfig) -> PolicyRules:
    record = await session.scalar(
        select(PolicyPack).where(
            PolicyPack.merchant_id == merchant.id,
            PolicyPack.version == merchant.active_policy_pack_version,
        )
    )
    if record is None:
        raise CommerceRuleViolation("policy_pack_not_found")
    try:
        return PolicyRules(
            max_quantity_per_line=int(record.rules["max_quantity_per_line"]),
            max_total_quantity=int(record.rules["max_total_quantity"]),
            approval_lifetime=timedelta(seconds=int(record.rules["approval_lifetime_seconds"])),
            inventory_lease_lifetime=timedelta(
                seconds=int(record.rules["inventory_lease_lifetime_seconds"])
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise CommerceRuleViolation("policy_pack_invalid") from None


async def _authoritative_price(
    session: AsyncSession,
    requested_lines: Sequence[RequestedLine],
    rules: PolicyRules,
    budget_minor: int | None,
) -> tuple[list[PricedLine], CheckoutPricing]:
    ids = sorted({line.variant_id for line in requested_lines})
    rows = await session.execute(
        select(Variant, Inventory, Product)
        .join(Inventory, Inventory.variant_id == Variant.id)
        .join(Product, Product.id == Variant.product_id)
        .where(Variant.id.in_(ids))
        .order_by(Variant.id)
    )
    variants = {
        variant.id: VariantTerms(
            id=variant.id,
            sku=variant.sku,
            product_name=product.name,
            size=variant.size,
            color=variant.color,
            unit_price_minor=variant.unit_price_minor,
            inventory_version=inventory.version,
            available_quantity=inventory.on_hand - inventory.reserved - inventory.sold,
            active=variant.active and product.active,
            currency=variant.currency,
        )
        for variant, inventory, product in rows
    }
    return price_checkout(
        requests=requested_lines,
        variants=variants,
        rules=rules,
        budget_minor=budget_minor,
    )


def _replace_lines(session: AsyncSession, checkout_id: str, lines: Sequence[PricedLine]) -> None:
    session.add_all(
        [
            CheckoutLine(
                checkout_id=checkout_id,
                position=position,
                variant_id=line.variant_id,
                quantity=line.quantity,
                product_name=line.product_name,
                sku=line.sku,
                size=line.size,
                color=line.color,
                unit_price_minor=line.unit_price_minor,
                inventory_version=line.inventory_version,
            )
            for position, line in enumerate(lines, start=1)
        ]
    )


async def _load_checkout(
    session: AsyncSession, record: CheckoutSession
) -> AuthoritativeCheckout | None:
    records = list(
        await session.scalars(
            select(CheckoutLine)
            .where(CheckoutLine.checkout_id == record.id)
            .order_by(CheckoutLine.position)
        )
    )
    if not records:
        return None
    projection = await session.get(UCPCheckout, record.id)
    if projection is None:
        return None
    lines = [
        PricedLine(
            variant_id=line.variant_id,
            sku=line.sku,
            product_name=line.product_name,
            size=line.size,
            color=line.color,
            quantity=line.quantity,
            unit_price_minor=line.unit_price_minor,
            line_total_minor=line.unit_price_minor * line.quantity,
            inventory_version=line.inventory_version,
        )
        for line in records
    ]
    order = await session.scalar(
        select(MerchantOrder).where(MerchantOrder.checkout_id == record.id)
    )
    confirmation = None
    if record.status == CheckoutStatus.COMPLETED.value and order is not None:
        origin = urlsplit(projection.continue_url)
        confirmation = {
            "id": str(order.id),
            "permalink_url": f"{origin.scheme}://{origin.netloc}/orders/{order.id}",
        }
    return _checkout_value(
        record, lines, continue_url=projection.continue_url, order_confirmation=confirmation
    )


def _checkout_value(
    record: CheckoutSession,
    lines: Sequence[PricedLine],
    *,
    continue_url: str,
    pricing: CheckoutPricing | None = None,
    order_confirmation: dict[str, str] | None = None,
) -> AuthoritativeCheckout:
    if pricing is None:
        item_total = sum(line.line_total_minor for line in lines)
        pricing = CheckoutPricing(
            currency="INR",
            item_total_minor=item_total,
            pickup_charge_minor=0,
            total_minor=item_total,
            tax_inclusive=True,
        )
    external_status = (
        "canceled" if record.status == CheckoutStatus.CANCELED.value else "requires_escalation"
    )
    if record.status == CheckoutStatus.COMPLETED.value and order_confirmation is not None:
        external_status = "completed"
    resource: dict[str, object] = {
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": {"dev.ucp.shopping.checkout": [{"version": UCP_VERSION}]},
            "payment_handlers": {},
        },
        "id": record.id,
        "status": external_status,
        "currency": pricing.currency,
        "checkout_version": record.version,
        "policy_pack_version": record.policy_pack_version,
        "line_items": [
            {
                "id": line.variant_id,
                "item": {
                    "id": line.variant_id,
                    "sku": line.sku,
                    "title": line.product_name,
                    "price": line.unit_price_minor,
                },
                "quantity": line.quantity,
                "totals": [
                    {"type": "subtotal", "amount": line.line_total_minor},
                    {"type": "total", "amount": line.line_total_minor},
                ],
                "unit_price": {
                    "currency": pricing.currency,
                    "minor_units": line.unit_price_minor,
                },
                "line_total": {
                    "currency": pricing.currency,
                    "minor_units": line.line_total_minor,
                },
                "attributes": {"size": line.size, "color": line.color},
                "inventory_version": line.inventory_version,
            }
            for line in lines
        ],
        "totals": [
            {"type": "subtotal", "amount": pricing.item_total_minor},
            {"type": "total", "amount": pricing.total_minor},
        ],
        "links": [],
        "pricing": {
            "currency": pricing.currency,
            "item_total_minor": pricing.item_total_minor,
            "pickup_charge_minor": pricing.pickup_charge_minor,
            "total_minor": pricing.total_minor,
            "tax_inclusive": pricing.tax_inclusive,
        },
        "fulfillment": {
            "type": "store_pickup",
            "location_id": record.pickup_location_id,
        },
        "messages": (
            [
                {
                    "type": "error",
                    "code": "merchant_review_required",
                    "content": "Continue in the merchant checkout to review and authorize payment.",
                    "severity": "requires_buyer_review",
                }
            ]
            if external_status == "requires_escalation"
            else []
        ),
        "continue_url": continue_url,
        "expires_at": record.expires_at.isoformat().replace("+00:00", "Z"),
    }
    if order_confirmation is not None:
        resource["order"] = order_confirmation
        resource.pop("continue_url", None)
    return AuthoritativeCheckout(
        id=record.id,
        buyer_key_id=record.buyer_key_id,
        status=CheckoutStatus(record.status),
        version=record.version,
        policy_pack_version=record.policy_pack_version,
        pickup_location_id=record.pickup_location_id,
        lines=tuple(lines),
        pricing=pricing,
        budget_minor=record.budget_minor,
        expires_at=record.expires_at,
        continue_url=continue_url,
        canonical_bytes=canonical_json_bytes(resource),
    )


def _resource_from_bytes(value: bytes) -> dict[str, object]:
    resource = orjson.loads(value)
    if not isinstance(resource, dict):
        raise ValueError("Checkout projection must be a JSON object")
    return resource


async def _append_audit(
    session: AsyncSession,
    checkout_id: str,
    event_type: str,
    payload: dict[str, object],
    evidence_source: str = "signed_ucp_request",
) -> None:
    sequence = await session.scalar(
        select(func.coalesce(func.max(AuditEvent.sequence), 0)).where(
            AuditEvent.aggregate_type == "checkout",
            AuditEvent.aggregate_id == checkout_id,
        )
    )
    session.add(
        AuditEvent(
            aggregate_type="checkout",
            aggregate_id=checkout_id,
            sequence=int(sequence or 0) + 1,
            event_type=event_type,
            payload=payload,
            evidence_source=evidence_source,
        )
    )


def _blocked(rule_id: str) -> CommerceMutationResult:
    return CommerceMutationResult(
        CommerceMutationOutcome.BLOCKED,
        rule_ids=(rule_id,),
    )
