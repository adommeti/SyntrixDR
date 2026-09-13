from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.core.clock import FakeClock
from app.core.config import get_settings

_API_DIR = Path(__file__).resolve().parents[1]


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    # `driver="psycopg"` (psycopg3) so the same URL works for both Alembic's
    # async env.py and the async engine below — no sync/async URL split needed.
    with PostgresContainer("pgvector/pgvector:0.8.6-pg16", driver="psycopg") as pg:
        url = pg.get_connection_url()
        command.upgrade(_alembic_config(url), "head")
        yield url


@pytest.fixture(scope="session")
def engine(pg_url: str) -> AsyncEngine:
    return create_async_engine(pg_url, pool_pre_ping=True)


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Every test runs inside a transaction rolled back at the end (tests.md rule)."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session_factory = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        async with session_factory() as sess:
            yield sess
        await trans.rollback()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def seed_user(session: AsyncSession) -> uuid.UUID:
    """Minimal `users` row so FK-bearing tables (idempotency_keys) can be exercised
    ahead of BUILD-02 (identity_auth), which owns the real `User` model."""
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )
    await session.flush()
    return user_id


@pytest.fixture
def idempotency_key() -> str:
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Connects to the local redis instance (assumes docker-compose is running).

    Tests that create keys should clean them up or use a unique prefix.
    """
    settings = get_settings()
    client = Redis.from_url(settings.redis_url, decode_responses=False)
    try:
        await client.ping()
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def local_user_with_password(session: AsyncSession) -> tuple[uuid.UUID, str]:
    """Create a LOCAL user with a known password for authentication testing.

    Returns:
        Tuple of (user_id, password)
    """
    from app.identity_auth.models import LocalCredential
    from app.identity_auth.security import PasswordHasher

    user_id = uuid.uuid4()
    email = f"{user_id}@example.test"
    password = "TestPassword123!"

    # Create user row
    await session.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email) "
            "VALUES (:id, 'LOCAL', 'Test User', :email)"
        ),
        {"id": user_id, "email": email},
    )
    await session.flush()

    # Create local_credentials via ORM
    hasher = PasswordHasher()
    password_hash = hasher.hash_password(password)
    cred = LocalCredential(user_id=user_id, password_hash=password_hash)
    session.add(cred)
    await session.flush()

    return user_id, password
