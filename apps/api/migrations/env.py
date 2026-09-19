from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.core.database import Base
from app.core.file_policy import FilePolicy
from app.core.idempotency import IdempotencyKey
from app.core.outbox import OutboxEvent
from app.plans_import.models import PlanVersion
from app.work_streams.models import WorkStream

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Referenced only to register their tables on `Base.metadata` before Alembic reads it.
# `PlanVersion` specifically: `DrEvent.baseline_plan_version_id` FKs to `plan_versions.id`
# (schema_v1.sql's deferred `fk_event_baseline` ALTER) and nothing else in this module's own
# import chain (`core.idempotency`/`core.outbox`) pulls `plans_import.models` in, so running
# `alembic check` without this explicit import fails to resolve that FK's target table.
# `WorkStream`/`FilePolicy` (BUILD-05): nothing yet imports `work_streams.models`/
# `core.file_policy` either, so their tables would be invisible to `alembic check` without this
# explicit registration.
_MODELED_TABLES = (IdempotencyKey, OutboxEvent, PlanVersion, WorkStream, FilePolicy)


def get_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _is_comparable_table(table_name: str | None) -> bool:
    """True only for fully modeled tables — excludes both un-modeled schema_v1/v2
    tables (their modules land in later BUILDs), FK-resolution stubs
    (`app.core.external_refs`, a single `id` column, not the real shape), and
    partial_read_only tables (owned by other modules, only queried here).
    Without this, `alembic check` would see every un-modeled table as a pending
    DROP and every stub as a pending ADD (D-247).
    """
    if table_name is None:
        return False
    table = target_metadata.tables.get(table_name)
    if table is None:
        return False
    return not table.info.get("fk_resolution_stub", False) and not table.info.get("partial_read_only", False)


def include_object(object_: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table":
        return _is_comparable_table(name)
    # For columns/indexes/constraints (including FKs), `object_.table` is the
    # OWNING table (e.g. idempotency_keys for its user_id -> users.id FK), not
    # the referenced one — so a FK into a stub table is still compared as long
    # as its owning table is fully modeled. Verified: `alembic check` stays
    # clean with idempotency_keys/outbox_events' FKs into the users/dr_events
    # stubs (apps/api/tests/test_migrations.py). Re-check this if a future
    # BUILD's model swaps `keep_existing=True` import order in a way that lets
    # a stub table win over the real one.
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
