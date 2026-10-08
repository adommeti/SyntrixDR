"""D-234: one canonical typed catalog in `packages/contracts` mirrored by backend enums. These tests pin
each backend enum to its contracts array, the outbox union to API_CONTRACT's V1 list, and every
`write_outbox` call site to the union."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.comments_mentions_notifications.catalog import AlertType, NotificationType, Severity
from app.core.catalogs import OutboxEventType, TimelineEventType
from app.core.clock import FakeClock
from app.core.outbox import write_outbox
from tests.factories import build_world

pytestmark = [pytest.mark.domain]

_ROOT = Path(__file__).resolve().parents[3]
_CATALOG = _ROOT / "packages" / "contracts" / "catalog.ts"
_CONTRACT = _ROOT / "docs" / "spec" / "API_CONTRACT.md"


def _array(name: str) -> list[str]:
    block = re.search(rf"export const {name} = \[(.*?)\] as const;", _CATALOG.read_text(), re.S)
    assert block is not None, f"{name} not found in packages/contracts/catalog.ts"
    return re.findall(r'"([A-Za-z_]+)"', block.group(1))


@pytest.mark.parametrize(
    ("name", "enum"),
    [
        ("SEVERITIES", Severity),
        ("ALERT_TYPES", AlertType),
        ("NOTIFICATION_TYPES", NotificationType),
        ("WS_EVENT_TYPES", OutboxEventType),
        ("TIMELINE_EVENT_TYPES", TimelineEventType),
    ],
)
def test_backend_enum_matches_the_contracts_catalog(name: str, enum: type[StrEnum]) -> None:
    assert [m.value for m in enum] == _array(name)


def test_the_ws_union_is_api_contracts_v1_union() -> None:
    line = next(li for li in _CONTRACT.read_text().splitlines() if li.startswith("The V1 union is:"))
    assert re.findall(r"`([A-Za-z]+)`", line) == [m.value for m in OutboxEventType]


def test_every_outbox_call_site_names_a_union_member() -> None:
    literals = set(
        re.findall(
            r'event_type="([A-Za-z]+)"',
            "".join(p.read_text() for p in (_ROOT / "apps/api/app").rglob("*.py")),
        )
    )
    assert literals and literals <= {m.value for m in OutboxEventType}


async def test_write_outbox_rejects_a_type_outside_the_union(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    with pytest.raises(ValueError):
        await write_outbox(
            session,
            aggregate_type="DR_EVENT",
            aggregate_id=w.event_id,
            event_type="SomethingNew",
            payload={},
            clock=clock,
        )
