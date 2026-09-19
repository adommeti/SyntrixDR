"""Pure query-layer test for `applications_catalog/queries.py::get_application_by_name`, added
for BUILD-05's Excel import row resolution (the `Application` column)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import get_application_by_name

pytestmark = [pytest.mark.domain]


async def _tier_id(session: AsyncSession, code: str) -> uuid.UUID:
    row = await session.execute(text("SELECT id FROM tiers WHERE code = :code"), {"code": code})
    return row.scalar_one()


async def _create_application(session: AsyncSession, *, name: str) -> uuid.UUID:
    tier_id = await _tier_id(session, "TIER_2")
    app_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO applications (id, name, tier_id) VALUES (:id, :name, :tier_id)"),
        {"id": app_id, "name": name, "tier_id": tier_id},
    )
    await session.flush()
    return app_id


@pytest.mark.asyncio
async def test_get_application_by_name_matches_case_insensitively(session: AsyncSession) -> None:
    app_id = await _create_application(session, name="Billing Platform")

    found = await get_application_by_name(session, "Billing Platform")
    assert found is not None
    assert found.id == app_id

    found = await get_application_by_name(session, "billing platform")
    assert found is not None
    assert found.id == app_id


@pytest.mark.asyncio
async def test_get_application_by_name_returns_none_when_not_found(session: AsyncSession) -> None:
    await _create_application(session, name="Billing Platform")

    assert await get_application_by_name(session, "Nonexistent App") is None
