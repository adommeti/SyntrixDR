from __future__ import annotations

from sqlalchemy import Column, Table
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from app.core.database import Base

# Lightweight FK-resolution stubs for tables whose owning module hasn't landed yet
# (tasks_dependencies -> `tasks`/`task_dependencies`; milestones -> `milestones`). Referenced by
# `plans_import.models`'s `PlanVersionTask.source_task_id` etc. `keep_existing=True` so each
# owning module's later, full model definition on this same `Base.metadata` wins without a
# "table already defined" error (mirrors the removed `dr_events`/`users` stubs' pattern from
# BUILD-01/02 — see git history — now needed again for these three tables).
tasks_table = Table(
    "tasks",
    Base.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    keep_existing=True,
    info={"fk_resolution_stub": True},
)

task_dependencies_table = Table(
    "task_dependencies",
    Base.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    keep_existing=True,
    info={"fk_resolution_stub": True},
)

milestones_table = Table(
    "milestones",
    Base.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    keep_existing=True,
    info={"fk_resolution_stub": True},
)
