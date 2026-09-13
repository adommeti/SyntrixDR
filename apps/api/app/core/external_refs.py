from __future__ import annotations

from sqlalchemy import Column, Enum, Table, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from app.core.database import Base

# Lightweight FK-resolution stubs for tables owned by modules that don't have a
# SQLAlchemy model yet (users_teams_org -> BUILD-02, dr_events -> BUILD-04).
# `keep_existing=True` so the owning module's later, full model definition on
# this same `Base.metadata` wins without a "table already defined" error.
users_table = Table(
    "users",
    Base.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    Column("email", Text, nullable=False),
    Column("identity_type", Enum("ENTRA", "LOCAL", name="identity_type", native_enum=True), nullable=False),
    Column("entra_object_id", Text, nullable=True),
    keep_existing=True,
    info={"fk_resolution_stub": True},
)

dr_events_table = Table(
    "dr_events",
    Base.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    keep_existing=True,
    info={"fk_resolution_stub": True},
)
