from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field, replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_browser_sessions import Store

from acsa.domain.payments import ProviderOrderRecord, ProviderPaymentRecord
from acsa.security.browser_sessions import BrowserAuthorization
from acsa.services.payment_finalization import (
    EvidenceSource,
    FinalizationAction,
    FinalizationOutcome,
    FinalizationWork,
    PaymentFinalizationService,
    PaymentLaunchConfiguration,
)
from acsa.web.payment_confirmation import create_payment_confirmation_router


def _work(action: FinalizationAction = FinalizationAction.FINALIZE) -> FinalizationWork:
    return FinalizationWork(
        action=action,
        attempt_id="att_1",
        checkout_id="chk_1",
        provider_order_id="order_1",
        receipt="acsa1_receipt",
        amount_minor=499_900,
        currency="INR",
        snapshot_checksum="a" * 64,
    )


def _payment(payment_id: str = "pay_1") -> ProviderPaymentRecord:
    return ProviderPaymentRecord(
        payment_id=payment_id,
        order_id="order_1",
        amount_minor=499_900,
        currency="INR",
        status="captured",
        captured=True,
    )


def _order() -> ProviderOrderRecord:
    return ProviderOrderRecord(
        order_id="order_1",
        receipt="acsa1_receipt",
        amount_minor=499_900,
        currency="INR",
        status="paid",
        notes={
            "checkout_id": "chk_1",
            "attempt_id": "att_1",
            "snapshot_checksum": "a" * 64,
        },
    )


@dataclass
class StoreStub:
    work: FinalizationWork = field(default_factory=_work)
    finalize_outcome: FinalizationOutcome = FinalizationOutcome.COMPLETED
    verifying: list[str] = field(default_factory=list)
    finalized: list[tuple[str, str, str]] = field(default_factory=list)
    reconciling: list[str] = field(default_factory=list)

    async def load_work(self, attempt_id: str) -> FinalizationWork:
        assert attempt_id == "att_1"
        return self.work

    async def mark_verifying(self, attempt_id: str) -> bool:
        self.verifying.append(attempt_id)
        return True

    async def finalize(
        self,
        attempt_id: str,
        *,
        provider_account_id: str,
        payment: ProviderPaymentRecord,
        evidence_source: EvidenceSource,
        evidence_digest: str,
    ) -> FinalizationOutcome:
        assert provider_account_id == "rzp_test_account"
        self.finalized.append((attempt_id, payment.payment_id, evidence_digest))
        return self.finalize_outcome

    async def mark_reconciling(self, attempt_id: str) -> None:
        self.reconciling.append(attempt_id)


@dataclass
class ProviderStub:
    payment: ProviderPaymentRecord = field(default_factory=_payment)
    order: ProviderOrderRecord = field(default_factory=_order)
    payment_fetches: list[str] = field(default_factory=list)
    order_fetches: list[str] = field(default_factory=list)

    async def fetch_payment(self, payment_id: str) -> ProviderPaymentRecord:
        self.payment_fetches.append(payment_id)
        return self.payment

    async def fetch_order(self, order_id: str) -> ProviderOrderRecord:
        self.order_fetches.append(order_id)
        return self.order


def _signature(payment_id: str = "pay_1") -> str:
    return hmac.new(
        b"fixture-secret",
        f"order_1|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        EvidenceSource.browser(payment_id="pay_tampered", signature=_signature()),
        EvidenceSource.browser(payment_id="pay_1", signature="0" * 64),
    ],
)
async def test_browser_tampering_is_rejected_before_provider_fetch(
    source: EvidenceSource,
) -> None:
    store = StoreStub()
    provider = ProviderStub()

    result = await PaymentFinalizationService(
        store=store,
        provider=provider,
        provider_account_id="rzp_test_account",
        checkout_secret="fixture-secret",
    ).finalize_payment("att_1", source)

    assert result is FinalizationOutcome.REJECTED
    assert provider.payment_fetches == []
    assert provider.order_fetches == []
    assert store.verifying == []


@pytest.mark.asyncio
async def test_exact_browser_evidence_is_fetched_then_finalized() -> None:
    store = StoreStub()
    provider = ProviderStub()
    source = EvidenceSource.browser(payment_id="pay_1", signature=_signature())

    result = await PaymentFinalizationService(
        store=store,
        provider=provider,
        provider_account_id="rzp_test_account",
        checkout_secret="fixture-secret",
    ).finalize_payment("att_1", source)

    assert result is FinalizationOutcome.COMPLETED
    assert provider.payment_fetches == ["pay_1"]
    assert provider.order_fetches == ["order_1"]
    assert store.verifying == ["att_1"]
    assert store.finalized[0][:2] == ("att_1", "pay_1")
    assert len(store.finalized[0][2]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payment", "order"),
    [
        (_payment("pay_other"), _order()),
        (replace(_payment(), order_id="order_other"), _order()),
        (replace(_payment(), status="authorized", captured=False), _order()),
        (_payment(), replace(_order(), status="created")),
        (_payment(), replace(_order(), receipt="wrong_receipt")),
        (_payment(), replace(_order(), amount_minor=1)),
        (_payment(), replace(_order(), currency="USD")),
        (
            _payment(),
            replace(
                _order(),
                notes={
                    "checkout_id": "chk_1",
                    "attempt_id": "att_1",
                    "snapshot_checksum": "b" * 64,
                },
            ),
        ),
    ],
)
async def test_conflicting_provider_evidence_moves_attempt_to_reconciliation(
    payment: ProviderPaymentRecord,
    order: ProviderOrderRecord,
) -> None:
    store = StoreStub()
    provider = ProviderStub(payment=payment, order=order)
    source = EvidenceSource.webhook(payment_id="pay_1", order_id="order_1")

    result = await PaymentFinalizationService(
        store=store,
        provider=provider,
        provider_account_id="rzp_test_account",
        checkout_secret="fixture-secret",
    ).finalize_payment("att_1", source)

    assert result is FinalizationOutcome.RECONCILING
    assert store.finalized == []
    assert store.reconciling == ["att_1"]


@pytest.mark.asyncio
async def test_transaction_conflict_moves_verifying_attempt_to_reconciliation() -> None:
    store = StoreStub(finalize_outcome=FinalizationOutcome.RECONCILING)

    result = await PaymentFinalizationService(
        store=store,
        provider=ProviderStub(),
        provider_account_id="rzp_test_account",
        checkout_secret="fixture-secret",
    ).finalize_payment(
        "att_1",
        EvidenceSource.webhook(payment_id="pay_1", order_id="order_1"),
    )

    assert result is FinalizationOutcome.RECONCILING
    assert store.reconciling == ["att_1"]


@dataclass
class RouteServiceStub:
    outcome: FinalizationOutcome = FinalizationOutcome.COMPLETED
    sources: list[tuple[str, EvidenceSource]] = field(default_factory=list)

    async def payment_launch_configuration(
        self, attempt_id: str
    ) -> PaymentLaunchConfiguration | None:
        if attempt_id != "att_1":
            return None
        return PaymentLaunchConfiguration(
            checkout_id="chk_1",
            attempt_id="att_1",
            provider_key_id="rzp_test_public",
            provider_order_id="order_1",
            amount_minor=499_900,
            currency="INR",
        )

    async def finalize_payment(
        self, attempt_id: str, source: EvidenceSource
    ) -> FinalizationOutcome:
        self.sources.append((attempt_id, source))
        return self.outcome


def _route_client(service: RouteServiceStub) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_payment_confirmation_router(
            service, BrowserAuthorization(Store(), "https://merchant.example")
        )
    )
    return TestClient(
        app,
        headers={
            "Authorization": "Bearer " + "session-" * 8,
            "Origin": "https://merchant.example",
            "X-CSRF-Token": "csrf-" * 8,
        },
    )


def test_payment_launch_configuration_exposes_public_fields_only() -> None:
    response = _route_client(RouteServiceStub()).get("/api/payments/razorpay/launch/att_1")

    assert response.status_code == 200
    assert response.json() == {
        "checkout_id": "chk_1",
        "attempt_id": "att_1",
        "key_id": "rzp_test_public",
        "order_id": "order_1",
        "amount": 499_900,
        "currency": "INR",
    }
    assert "secret" not in response.text.lower()


def test_payment_confirmation_routes_browser_evidence_to_the_finalizer() -> None:
    service = RouteServiceStub()

    response = _route_client(service).post(
        "/api/payments/razorpay/confirm",
        json={
            "attempt_id": "att_1",
            "razorpay_payment_id": "pay_1",
            "razorpay_signature": "f" * 64,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "completed"}
    assert service.sources == [
        (
            "att_1",
            EvidenceSource.browser(payment_id="pay_1", signature="f" * 64),
        )
    ]
