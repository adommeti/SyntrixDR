from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.tooling import seed as seed_module
from app.tooling.export_openapi import export_openapi
from app.tooling.seed import seed_golden

pytestmark = pytest.mark.api


def test_export_openapi_writes_valid_document(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"

    export_openapi(out)

    schema = json.loads(out.read_text())
    assert "openapi" in schema
    assert "paths" in schema


@pytest.mark.asyncio
async def test_seed_refuses_outside_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-235: no pre-created production Local account — the seed script must
    refuse to run anywhere but DRCC_ENV=local, before it ever touches the DB."""
    monkeypatch.setattr(seed_module, "get_settings", lambda: Settings(drcc_env="staging"))

    with pytest.raises(RuntimeError, match="DRCC_ENV=local"):
        await seed_module.run("golden")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_golden_is_idempotent(engine: AsyncEngine) -> None:
    """Runs against its own rolled-back transaction on the shared test engine
    so it never leaks a GLOBAL_ADMIN row into other tests in this session.
    """
    count_sql = text(
        "SELECT count(*) FROM role_assignments "
        "WHERE role_key = 'GLOBAL_ADMIN' AND scope_type = 'GLOBAL' AND revoked_at IS NULL"
    )

    async with engine.connect() as conn:
        trans = await conn.begin()

        assert (await conn.execute(count_sql)).scalar_one() == 0

        await seed_golden(conn)
        assert (await conn.execute(count_sql)).scalar_one() == 1

        await seed_golden(conn)
        assert (await conn.execute(count_sql)).scalar_one() == 1

        await trans.rollback()
