from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None) -> AsyncEngine:
    settings: Settings = get_settings()
    return create_async_engine(database_url or settings.database_url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def get_request_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, bound to `app.state.session_factory`."""
    async with request.app.state.session_factory() as session:
        yield session
