from __future__ import annotations

from sqlalchemy import Column, Table
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from app.core.database import Base

# Lightweight FK-resolution stub for tables whose owning module hasn't landed yet
# (milestones -> `milestones`). Referenced by `plans_import.models`'s
# `PlanVersionMilestone.source_milestone_id`. `keep_existing=True` so the owning module's later,
# full model definition on this same `Base.metadata` wins without a "table already defined" error
# (mirrors the removed `dr_events`/`users` stubs' pattern from BUILD-01/02, and `tasks`/
# `task_dependencies`' own removal in BUILD-05 once `tasks_dependencies.models` became real —
# see git history).
milestones_table = Table(
    "milestones",
    Base.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    keep_existing=True,
    info={"fk_resolution_stub": True},
)
