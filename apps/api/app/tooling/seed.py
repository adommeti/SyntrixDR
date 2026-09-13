from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import make_engine
from app.core.security import hash_password

DEV_ADMIN_EMAIL = "admin@drcc.local"
DEV_ADMIN_PASSWORD = "ChangeMe123!ChangeMe"  # dev-only; never used outside DRCC_ENV=local (D-235)


async def seed_golden(conn: AsyncConnection) -> None:
    """Tiers are already seeded by 0001_schema_v1.sql. This scenario only creates
    one Local GLOBAL_ADMIN so a fresh clone can log in locally (BUILD-02 wires
    the actual login flow; this just makes the row exist). Transaction lifecycle
    is the caller's responsibility (CLI commits; tests roll back).
    """
    existing = (
        await conn.execute(
            text(
                "SELECT 1 FROM role_assignments "
                "WHERE role_key = 'GLOBAL_ADMIN' AND scope_type = 'GLOBAL' AND revoked_at IS NULL "
                "LIMIT 1"
            )
        )
    ).first()
    if existing is not None:
        print("seed: a GLOBAL_ADMIN already exists, skipping (idempotent)")
        return

    user_id = uuid.uuid4()
    now = SystemClock().now()
    await conn.execute(
        text(
            "INSERT INTO users (id, identity_type, display_name, email, created_at, updated_at) "
            "VALUES (:id, 'LOCAL', 'Dev Global Admin', :email, :now, :now)"
        ),
        {"id": user_id, "email": DEV_ADMIN_EMAIL, "now": now},
    )
    await conn.execute(
        text(
            "INSERT INTO local_credentials (id, user_id, password_hash, password_changed_at) "
            "VALUES (:id, :user_id, :password_hash, :now)"
        ),
        {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "password_hash": hash_password(DEV_ADMIN_PASSWORD),
            "now": now,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO role_assignments (id, user_id, role_key, scope_type, scope_id, created_at) "
            "VALUES (:id, :user_id, 'GLOBAL_ADMIN', 'GLOBAL', NULL, :now)"
        ),
        {"id": uuid.uuid4(), "user_id": user_id, "now": now},
    )
    print(f"seed: created Local GLOBAL_ADMIN {DEV_ADMIN_EMAIL} (password: {DEV_ADMIN_PASSWORD})")


async def run(scenario: str) -> None:
    settings = get_settings()
    if settings.drcc_env != "local":
        raise RuntimeError(
            f"seed refuses to run outside DRCC_ENV=local (got {settings.drcc_env!r}) — "
            "no pre-created production Local account (D-235)."
        )

    if scenario != "golden":
        raise NotImplementedError(
            f"seed scenario {scenario!r} is not implemented yet (BUILD-01 only ships 'golden')."
        )

    engine: AsyncEngine = make_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await seed_golden(conn)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed fictitious dev/test data (D-236).")
    parser.add_argument("--scenario", required=True, choices=["golden", "load"])
    parser.add_argument("--applications", type=int, default=None)
    parser.add_argument("--tasks", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run(args.scenario))


if __name__ == "__main__":
    main()
