"""Test configuration and fixtures."""

import pytest
from typing import AsyncGenerator, Generator
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from src.main import app
from src.database import Base, get_db


# Test database URL (in-memory SQLite for speed)
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


@pytest.fixture(scope="function")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create a new database session for a test."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(scope="function")
def client(db_session: AsyncSession) -> Generator[TestClient, None, None]:
    """Create a test client with a mock database.

    override_get_db is an async generator that yields the SAME db_session
    for every HTTP request. FastAPI calls get_db() and does `async for
    value in get_db()`. Each call creates a new generator, but all
    generators yield the identical session object, so commits from one
    request are visible to the next.
    """
    async def override_get_db():
        await db_session.rollback()  # reset session state between requests
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app=app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()
