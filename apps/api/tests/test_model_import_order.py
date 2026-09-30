"""ADR-038: modules pre-built this increment import each other's ORM classes purely to register FK
targets on `Base.metadata`. Import them in every order in a *fresh interpreter* -- the test process
has already imported everything, so an in-process check would pass vacuously -- and configure all
mappers; a missing registration surfaces as NoReferencedTableError / mapper configuration errors."""

from __future__ import annotations

import itertools
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain]

_API_DIR = Path(__file__).resolve().parents[1]
#: The tables this increment models. Only *their* FK targets are checked: dr_events' own
#: baseline_plan_version_id -> plan_versions FK is BUILD-04's documented circular pair, registered by
#: plans_import rather than by dr_events.models (.claude/rules/dr-events.md).
OWNED_TABLES = {
    "tasks",
    "task_dependencies",
    "milestone_dependencies",
    "blockers",
    "validations",
    "work_streams",
}
MODULES = [
    "app.blockers.models",
    "app.validation_evidence.models",
    "app.tasks_dependencies.models",
    "app.work_streams.models",
]


@pytest.mark.parametrize(
    "order", list(itertools.permutations(MODULES)), ids=lambda o: ">".join(m.split(".")[1] for m in o)
)
def test_models_register_and_configure_in_any_import_order(order: tuple[str, ...]) -> None:
    script = "; ".join(f"import {m}" for m in order) + (
        "; from sqlalchemy.orm import configure_mappers; configure_mappers()"
        "; from app.core.database import Base"
        f"; owned = {sorted(OWNED_TABLES)!r}"
        "; missing = [fk.target_fullname for name in owned for fk in Base.metadata.tables[name].foreign_keys"
        " if fk.target_fullname.split('.')[0] not in Base.metadata.tables]"
        "; assert not missing, missing"
        "; [fk.column for name in owned for fk in Base.metadata.tables[name].foreign_keys]"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=_API_DIR, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]
