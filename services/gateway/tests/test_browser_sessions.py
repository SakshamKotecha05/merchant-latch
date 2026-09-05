from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from acsa.security.browser_sessions import BrowserAuthorization, BrowserIdentity, token_digest


class Store:
    async def authenticate(self, digest):
        if digest != token_digest("session-" * 8):
            return None
        now = datetime.now(UTC)
        return BrowserIdentity(
            "chk_1",
            1,
            now,
            now + timedelta(minutes=10),
            now + timedelta(hours=1),
            token_digest("csrf-" * 8),
        )

    async def owns_attempt(self, checkout_id, attempt_id):
        return checkout_id == "chk_1" and attempt_id == "att_1"


def client():
    app = FastAPI()
    auth = BrowserAuthorization(Store(), "https://merchant.example")

    @app.api_route("/attempts/{attempt_id}", methods=["GET", "POST"])
    async def attempt(attempt_id: str, request: Request):
        identity = await auth.require(request, attempt_id=attempt_id)
        return {"checkout": identity.checkout_id}

    return TestClient(app)


@pytest.mark.parametrize("attempt,status", [("att_1", 200), ("att_other", 404)])
def test_session_is_bound_to_one_checkout(attempt, status):
    assert (
        client()
        .get(f"/attempts/{attempt}", headers={"Authorization": "Bearer " + "session-" * 8})
        .status_code
        == status
    )


@pytest.mark.parametrize(
    "origin,csrf,status",
    [
        ("https://merchant.example", "csrf-" * 8, 200),
        ("https://evil.example", "csrf-" * 8, 403),
        ("https://merchant.example", "wrong-" * 8, 403),
    ],
)
def test_mutations_require_origin_and_csrf(origin, csrf, status):
    assert (
        client()
        .post(
            "/attempts/att_1",
            headers={
                "Authorization": "Bearer " + "session-" * 8,
                "Origin": origin,
                "X-CSRF-Token": csrf,
            },
        )
        .status_code
        == status
    )
