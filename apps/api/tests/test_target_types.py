"""D-216: one canonical `target_type` enum, mirrored by `packages/contracts` (D-234), with per-table
subsets enforced by CHECK and/or service. These tests pin all three copies of the truth -- backend
enum, contracts catalog, live database -- to each other."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import OutboxEvent
from app.core.target_types import TARGET_TYPE_SUBSETS, TargetType, target_type_enum
from app.dr_events.models import Override
from app.plans_import.models import NeedsReviewItem

pytestmark = [pytest.mark.domain]

_CATALOG = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "catalog.ts"


def test_backend_enum_matches_the_contracts_catalog() -> None:
    block = re.search(r"export const TARGET_TYPES = \[(.*?)\] as const;", _CATALOG.read_text(), re.S)
    assert block is not None, "TARGET_TYPES not found in packages/contracts/catalog.ts"
    catalog = re.findall(r'"([A-Z_]+)"', block.group(1))

    assert [t.value for t in TargetType] == catalog


async def test_backend_enum_matches_the_database_enum(session: AsyncSession) -> None:
    labels = (
        (await session.execute(text("SELECT unnest(enum_range(NULL::target_type))::text"))).scalars().all()
    )

    assert [t.value for t in TargetType] == list(labels)


@pytest.mark.parametrize("table", sorted(TARGET_TYPE_SUBSETS))
async def test_each_subset_matches_its_database_check(session: AsyncSession, table: str) -> None:
    definition = await session.scalar(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
        {"name": f"{table}_target_type_ck"},
    )
    assert definition is not None, f"{table}_target_type_ck missing"

    assert set(re.findall(r"'([A-Z_]+)'", definition)) == {t.value for t in TARGET_TYPE_SUBSETS[table]}


async def test_tables_without_a_subset_have_no_check(session: AsyncSession) -> None:
    """overrides/alerts/notifications take any label (schema_v2_reconciliation.sql:157)."""
    for table in ("overrides", "alerts", "notifications"):
        assert table not in TARGET_TYPE_SUBSETS
        assert (
            await session.scalar(
                text("SELECT count(*) FROM pg_constraint WHERE conname = :name"),
                {"name": f"{table}_target_type_ck"},
            )
            == 0
        )


def test_every_modeled_column_shares_the_one_enum() -> None:
    assert Override.__table__.c.target_type.type is target_type_enum
    assert NeedsReviewItem.__table__.c.target_type.type is target_type_enum
    assert OutboxEvent.__table__.c.aggregate_type.type is target_type_enum
