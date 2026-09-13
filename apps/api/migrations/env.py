from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.core.database import Base
from app.core.idempotency import IdempotencyKey
from app.core.outbox import OutboxEvent

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Referenced only to register their tables on `Base.metadata` before Alembic reads it.
_MODELED_TABLES = (IdempotencyKey, OutboxEvent)


def get_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _is_comparable_table(table_name: str | None) -> bool:
    """True only for fully modeled tables — excludes both un-modeled schema_v1/v2
    tables (their modules land in later BUILDs) and FK-resolution stubs
    (`app.core.external_refs`, a single `id` column, not the real shape).
    Without this, `alembic check` would see every un-modeled table as a pending
    DROP and every stub as a pending ADD (D-247).
    """
    if table_name is None:
        return False
    table = target_metadata.tables.get(table_name)
    if table is None:
        return False
    return not table.info.get("fk_resolution_stub", False)


def include_object(object_: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table":
        return _is_comparable_table(name)
    table = getattr(object_, "table", None)
    if table is not None:
        return _is_comparable_table(table.name)
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_async_engine(get_url(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
