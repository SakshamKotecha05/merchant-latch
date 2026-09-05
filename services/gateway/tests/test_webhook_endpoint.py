from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from acsa.ports.jobs import JobDispatcherPort
from acsa.ports.webhooks import WebhookInsertResult, WebhookStorePort
from acsa.web.razorpay_webhooks import MAX_WEBHOOK_BYTES, create_razorpay_webhook_router


@dataclass
class RecordingStore(WebhookStorePort):
    result: WebhookInsertResult
    calls: list[tuple[str, str, bytes]] = field(default_factory=list)

    async def insert_verified_event(
        self,
        *,
        event_id: str,
        event_name: str,
        raw_payload: bytes,
        payload_hash: str,
    ) -> WebhookInsertResult:
        self.calls.append((event_id, event_name, raw_payload))
        assert payload_hash == hashlib.sha256(raw_payload).hexdigest()
        return self.result


@dataclass
class RecordingDispatcher(JobDispatcherPort):
    fail: bool = False
    calls: list[UUID] = field(default_factory=list)

    async def dispatch(self, job_id: UUID) -> None:
        self.calls.append(job_id)
        if self.fail:
            raise TimeoutError("simulated dispatch timeout")


@dataclass
class FailingDispatcher(JobDispatcherPort):
    calls: list[UUID] = field(default_factory=list)

    async def dispatch(self, job_id: UUID) -> None:
        self.calls.append(job_id)
        raise RuntimeError("simulated non-timeout dispatch failure")


@dataclass
class TimedOutDispatcher(JobDispatcherPort):
    calls: list[UUID] = field(default_factory=list)

    async def dispatch(self, job_id: UUID) -> None:
        self.calls.append(job_id)
        await asyncio.Event().wait()


def signed_headers(raw_body: bytes, *, event_id: str = "evt_fixture") -> dict[str, str]:
    signature = hmac.new(b"fixture-webhook-secret", raw_body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-razorpay-event-id": event_id,
        "x-razorpay-signature": signature,
    }


def build_client(
    store: WebhookStorePort,
    dispatcher: JobDispatcherPort,
    *,
    raise_server_exceptions: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_razorpay_webhook_router(
            webhook_secret="fixture-webhook-secret",
            store=store,
            dispatcher=dispatcher,
            dispatch_timeout_seconds=0.05,
        )
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_verified_webhook_is_committed_before_dispatch() -> None:
    job_id = uuid4()
    store = RecordingStore(WebhookInsertResult(created=True, job_id=job_id))
    dispatcher = RecordingDispatcher()
    raw_body = b'{"event":"payment.captured","payload":{"payment":{"entity":{}}}}'

    response = build_client(store, dispatcher).post(
        "/webhooks/razorpay",
        content=raw_body,
        headers=signed_headers(raw_body),
    )

    assert response.status_code == 204
    assert store.calls == [("evt_fixture", "payment.captured", raw_body)]
    assert dispatcher.calls == [job_id]


def test_dispatch_failure_after_commit_still_acknowledges_webhook(
    caplog,
) -> None:  # type: ignore[no-untyped-def]
    job_id = uuid4()
    store = RecordingStore(WebhookInsertResult(created=True, job_id=job_id))
    dispatcher = RecordingDispatcher(fail=True)
    raw_body = b'{"event":"order.paid","payload":{}}'

    with caplog.at_level(logging.WARNING, logger="acsa.web.razorpay_webhooks"):
        response = build_client(store, dispatcher).post(
            "/webhooks/razorpay",
            content=raw_body,
            headers=signed_headers(raw_body),
        )

    assert response.status_code == 204
    assert len(store.calls) == 1
    assert dispatcher.calls == [job_id]
    assert all("job_id" not in record.__dict__ for record in caplog.records)


def test_dispatch_timeout_after_commit_still_acknowledges_webhook() -> None:
    job_id = uuid4()
    store = RecordingStore(WebhookInsertResult(created=True, job_id=job_id))
    dispatcher = TimedOutDispatcher()
    raw_body = b'{"event":"order.paid","payload":{}}'

    response = build_client(store, dispatcher).post(
        "/webhooks/razorpay",
        content=raw_body,
        headers=signed_headers(raw_body),
    )

    assert response.status_code == 204
    assert len(store.calls) == 1
    assert dispatcher.calls == [job_id]


def test_non_timeout_dispatch_failure_after_commit_still_acknowledges_webhook() -> None:
    job_id = uuid4()
    store = RecordingStore(WebhookInsertResult(created=True, job_id=job_id))
    dispatcher = FailingDispatcher()
    raw_body = b'{"event":"order.paid","payload":{}}'

    response = build_client(
        store,
        dispatcher,
        raise_server_exceptions=False,
    ).post(
        "/webhooks/razorpay",
        content=raw_body,
        headers=signed_headers(raw_body),
    )

    assert response.status_code == 204
    assert len(store.calls) == 1
    assert dispatcher.calls == [job_id]


def test_duplicate_webhook_is_acknowledged_without_second_dispatch() -> None:
    store = RecordingStore(WebhookInsertResult(created=False, job_id=None))
    dispatcher = RecordingDispatcher()
    raw_body = b'{"event":"payment.failed","payload":{}}'

    response = build_client(store, dispatcher).post(
        "/webhooks/razorpay",
        content=raw_body,
        headers=signed_headers(raw_body),
    )

    assert response.status_code == 204
    assert len(store.calls) == 1
    assert dispatcher.calls == []


def test_invalid_signature_fails_before_storage() -> None:
    store = RecordingStore(WebhookInsertResult(created=True, job_id=uuid4()))
    dispatcher = RecordingDispatcher()
    raw_body = b'{"event":"payment.captured","payload":{}}'
    headers = signed_headers(raw_body)
    headers["x-razorpay-signature"] = "0" * 64

    response = build_client(store, dispatcher).post(
        "/webhooks/razorpay",
        content=raw_body,
        headers=headers,
    )

    assert response.status_code == 401
    assert store.calls == []
    assert dispatcher.calls == []


def test_non_ascii_signature_fails_before_storage() -> None:
    store = RecordingStore(WebhookInsertResult(created=True, job_id=uuid4()))
    dispatcher = RecordingDispatcher()
    raw_body = b'{"event":"payment.captured","payload":{}}'

    response = build_client(store, dispatcher, raise_server_exceptions=False).post(
        "/webhooks/razorpay",
        content=raw_body,
        headers=[
            (b"content-type", b"application/json"),
            (b"x-razorpay-event-id", b"evt_fixture"),
            (b"x-razorpay-signature", b"\xff"),
        ],
    )

    assert response.status_code == 401
    assert store.calls == []
    assert dispatcher.calls == []


def test_payload_larger_than_one_mib_fails_before_storage() -> None:
    store = RecordingStore(WebhookInsertResult(created=True, job_id=uuid4()))
    dispatcher = RecordingDispatcher()
    raw_body = b"x" * (MAX_WEBHOOK_BYTES + 1)

    response = build_client(store, dispatcher).post(
        "/webhooks/razorpay",
        content=raw_body,
        headers=signed_headers(raw_body),
    )

    assert response.status_code == 413
    assert store.calls == []
    assert dispatcher.calls == []


def test_missing_event_id_fails_closed() -> None:
    store = RecordingStore(WebhookInsertResult(created=True, job_id=uuid4()))
    dispatcher = RecordingDispatcher()
    raw_body = b'{"event":"payment.captured","payload":{}}'
    headers = signed_headers(raw_body)
    del headers["x-razorpay-event-id"]

    response = build_client(store, dispatcher).post(
        "/webhooks/razorpay",
        content=raw_body,
        headers=headers,
    )

    assert response.status_code == 400
    assert store.calls == []
    assert dispatcher.calls == []
