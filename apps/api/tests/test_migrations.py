from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[1]


def _config(url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_downgrade_and_drift_check_from_empty_db() -> None:
    with PostgresContainer("pgvector/pgvector:0.8.6-pg16", driver="psycopg") as pg:
        cfg = _config(pg.get_connection_url())

        command.upgrade(cfg, "head")
        command.check(cfg)  # alembic check: model/migration drift gate (D-247)
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
