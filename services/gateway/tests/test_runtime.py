from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixture_keys import merchant_private_key, private_key_pem


@pytest.mark.filterwarnings(
    "ignore:Support for class-based `config` is deprecated:DeprecationWarning"
)
def test_vercel_entrypoint_composes_a_secret_safe_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    merchant_key_pem = private_key_pem(merchant_private_key())
    environment = {
        "DATABASE_URL": "postgresql://runtime-user:runtime-password@db.example/acsa",
        "DATABASE_DIRECT_URL": "postgresql+psycopg://owner-user:owner-password@db.example/acsa",
        "RAZORPAY_KEY_ID": "rzp_test_fixture_key",
        "RAZORPAY_KEY_SECRET": "fixture-provider-secret",
        "RAZORPAY_WEBHOOK_SECRET": "fixture-webhook-secret",
        "INNGEST_EVENT_KEY": "fixture-event-key",
        "INNGEST_SIGNING_KEY": "signkey-test-fixture",
        "UCP_INSPECTOR_TOKEN": "fixture-inspector-token-at-least-32-characters",
        "UCP_MERCHANT_PRIVATE_KEY": merchant_key_pem,
        "UCP_MERCHANT_KEY_ID": "merchant-p256-2026-01",
        "PUBLIC_GATEWAY_URL": "https://gateway.example",
        "PUBLIC_MERCHANT_URL": "https://merchant.example",
        "INNGEST_DEV": "1",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delitem(sys.modules, "app", raising=False)

    entrypoint = Path(__file__).parents[1] / "app.py"
    spec = spec_from_file_location("app", entrypoint)
    assert spec is not None
    assert spec.loader is not None
    runtime = module_from_spec(spec)
    sys.modules["app"] = runtime
    try:
        spec.loader.exec_module(runtime)
        response = TestClient(runtime.app).get("/api/inngest")

        assert response.status_code == 200
        assert response.json()["function_count"] == 3
        assert all(
            value not in response.text
            for name, value in environment.items()
            if name != "INNGEST_DEV"
        )
    finally:
        sys.modules.pop("app", None)
