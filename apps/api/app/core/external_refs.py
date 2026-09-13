from __future__ import annotations

from sqlalchemy import Column, Table
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from app.core.database import Base

# Lightweight FK-resolution stub for `dr_events` (owned by the dr_events module -> BUILD-04, not
# built yet — session b (BUILD-02) only builds the `dr_event_participants` slice of that module).
# `keep_existing=True` so the owning module's later, full model definition on this same
# `Base.metadata` wins without a "table already defined" error.
#
# `users` had an equivalent stub here through BUILD-02 session a; it's gone now that
# `app.users_teams_org.models.User` is the real, canonical definition (session b) — anything
# needing `users` for FK resolution imports that model directly instead.
dr_events_table = Table(
    "dr_events",
    Base.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    keep_existing=True,
    info={"fk_resolution_stub": True},
)
