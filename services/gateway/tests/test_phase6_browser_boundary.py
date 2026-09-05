from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_payment_finalization import RouteServiceStub

from acsa.web.payment_confirmation import create_payment_confirmation_router


def test_payment_routes_fail_closed_without_browser_authorization():
    app = FastAPI()
    service = RouteServiceStub()
    app.include_router(create_payment_confirmation_router(service))
    client = TestClient(app)
    assert client.get("/api/payments/razorpay/launch/att_1").status_code == 401
    assert (
        client.post(
            "/api/payments/razorpay/confirm",
            json={
                "attempt_id": "att_1",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": "f" * 64,
            },
        ).status_code
        == 401
    )
    assert service.sources == []
