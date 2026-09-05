from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from acsa.adapters.postgres.models import Base


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    owner_url = os.environ.get("TEST_DATABASE_DIRECT_URL")
    runtime_url = os.environ.get("TEST_DATABASE_URL")
    if not owner_url or not runtime_url:
        pytest.skip("TEST_DATABASE_URL and TEST_DATABASE_DIRECT_URL are not configured")

    owner_engine = create_async_engine(owner_url)
    runtime_engine = create_async_engine(runtime_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(runtime_engine, expire_on_commit=False)
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()
