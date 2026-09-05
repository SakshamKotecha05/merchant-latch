import os

import httpx
import pytest
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from fastapi import FastAPI

from acsa.web.operator import create_operator_router

pytestmark = pytest.mark.integration


def client(session_factory):
    password_hash = Argon2id(
        salt=os.urandom(16), length=32, iterations=2, lanes=1, memory_cost=8192
    ).derive_phc_encoded(b"local-operator-password")
    app = FastAPI()
    app.include_router(
        create_operator_router(session_factory, "https://merchant.example", password_hash)
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://merchant.example",
        headers={"Origin": "https://merchant.example"},
    )


async def test_operator_login_evidence_logout_and_revocation(session_factory):
    async with client(session_factory) as browser:
        assert (await browser.get("/internal/merchant/overview")).status_code == 401
        assert (
            await browser.post("/internal/merchant/login", json={"password": "wrong"})
        ).status_code == 401
        login = await browser.post(
            "/internal/merchant/login", json={"password": "local-operator-password"}
        )
        assert login.status_code == 200
        credentials = login.json()
        browser.headers.update(
            {
                "Authorization": "Bearer " + credentials["session"],
                "X-CSRF-Token": credentials["csrf"],
            }
        )
        overview = await browser.get("/internal/merchant/overview")
        assert overview.status_code == 200
        assert overview.json()["queue"]["pending"] == 0
        assert "password" not in overview.text
        assert (
            await browser.post(
                "/internal/merchant/logout", headers={"Origin": "https://evil.example"}
            )
        ).status_code == 403
        assert (await browser.post("/internal/merchant/logout")).status_code == 204
        assert (await browser.get("/internal/merchant/overview")).status_code == 401


async def test_operator_password_attempts_are_durably_bounded(session_factory):
    async with client(session_factory) as browser:
        codes = [
            (await browser.post("/internal/merchant/login", json={"password": "wrong"})).status_code
            for _ in range(6)
        ]
        assert codes == [401, 401, 401, 401, 401, 429]


async def test_operator_validation_never_echoes_password(session_factory):
    secret = "sensitive-password-" * 20
    async with client(session_factory) as browser:
        response = await browser.post("/internal/merchant/login", json={"password": secret})
    assert response.status_code == 400
    assert secret not in response.text
