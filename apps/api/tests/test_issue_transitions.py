"""Issue/Finding lifecycle (STATE_MACHINES.md §Issue/Finding, D-253):
`OPEN -> IN_REVIEW -> RESOLVED | DISMISSED`. Resolve and dismiss need a note.
An Issue never stops execution."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.issues_findings.commands import create_issue
from app.issues_findings.transition_service import IssueTransitionService
from app.tasks_dependencies.queries import get_task, readiness
from app.tasks_dependencies.transition_service import TaskTransitionService
from tests.factories import (
    audit_actions,
    build_world,
    issue_row,
    outbox_types,
    seed_issue,
    seed_task,
    task_row,
)

pytestmark = [pytest.mark.transitions]

OPEN, IN_REVIEW, RESOLVED, DISMISSED = "OPEN", "IN_REVIEW", "RESOLVED", "DISMISSED"
ALL = (OPEN, IN_REVIEW, RESOLVED, DISMISSED)
LEGAL_FROM = {"review": {OPEN}, "resolve": {OPEN, IN_REVIEW}, "dismiss": {OPEN, IN_REVIEW}}
TARGET = {"review": IN_REVIEW, "resolve": RESOLVED, "dismiss": DISMISSED}
ACTION = {"review": "ISSUE_IN_REVIEW", "resolve": "ISSUE_RESOLVED", "dismiss": "ISSUE_DISMISSED"}


async def _run(
    session: AsyncSession,
    clock: FakeClock,
    command: str,
    *,
    actor_id: uuid.UUID,
    issue_id: uuid.UUID,
    version: int = 1,
    **kw: Any,
) -> Any:
    kwargs: dict[str, Any] = {"note": "done"} if command != "review" else {}
    kwargs.update(kw)
    return await getattr(IssueTransitionService, command)(
        session, actor_id=actor_id, issue_id=issue_id, expected_version=version, clock=clock, **kwargs
    )


@pytest.mark.parametrize(("command", "from_state"), [(c, s) for c in LEGAL_FROM for s in LEGAL_FROM[c]])
async def test_legal_transition(
    session: AsyncSession, clock: FakeClock, command: str, from_state: str
) -> None:
    w = await build_world(session)
    issue_id = await seed_issue(session, w, status=from_state)

    await _run(session, clock, command, actor_id=w.coordinator_id, issue_id=issue_id)

    row = await issue_row(session, issue_id)
    assert (row.status, row.version) == (TARGET[command], 2)
    assert await audit_actions(session, issue_id) == [ACTION[command]]
    assert await outbox_types(session, issue_id) == ["IssueChanged"]
    if command != "review":
        assert (row.resolution_note, row.resolved_at) == ("done", clock.now())


@pytest.mark.parametrize(
    ("command", "from_state"), [(c, s) for c in LEGAL_FROM for s in ALL if s not in LEGAL_FROM[c]]
)
async def test_illegal_transition_is_rejected_and_nothing_changes(
    session: AsyncSession, clock: FakeClock, command: str, from_state: str
) -> None:
    w = await build_world(session)
    issue_id = await seed_issue(session, w, status=from_state)

    with pytest.raises(AppError) as e:
        await _run(session, clock, command, actor_id=w.coordinator_id, issue_id=issue_id)

    assert (e.value.code, e.value.status_code) == ("INVALID_TRANSITION", 409)
    assert (await issue_row(session, issue_id)).version == 1
    assert await audit_actions(session, issue_id) == []


@pytest.mark.parametrize("command", ["resolve", "dismiss"])
@pytest.mark.parametrize("note", [None, "", "   "])
async def test_resolve_and_dismiss_require_a_note(
    session: AsyncSession, clock: FakeClock, command: str, note: str | None
) -> None:
    w = await build_world(session)
    issue_id = await seed_issue(session, w)

    with pytest.raises(AppError) as e:
        await _run(session, clock, command, actor_id=w.coordinator_id, issue_id=issue_id, note=note)

    assert (e.value.code, e.value.status_code) == ("RESOLUTION_NOTE_REQUIRED", 422)
    assert (await issue_row(session, issue_id)).status == OPEN


@pytest.mark.parametrize("command", list(LEGAL_FROM))
async def test_stale_version_is_409(session: AsyncSession, clock: FakeClock, command: str) -> None:
    w = await build_world(session)
    issue_id = await seed_issue(session, w)
    with pytest.raises(AppError) as e:
        await _run(session, clock, command, actor_id=w.coordinator_id, issue_id=issue_id, version=9)
    assert e.value.code == "CONCURRENCY_CONFLICT"


@pytest.mark.parametrize("command", list(LEGAL_FROM))
async def test_invisible_event_is_404(session: AsyncSession, clock: FakeClock, command: str) -> None:
    w = await build_world(session)
    issue_id = await seed_issue(session, w)
    with pytest.raises(AppError) as e:
        await _run(session, clock, command, actor_id=w.stranger_id, issue_id=issue_id)
    assert (e.value.code, e.value.status_code) == ("ISSUE_NOT_FOUND", 404)
    with pytest.raises(AppError) as e:
        await _run(session, clock, command, actor_id=w.coordinator_id, issue_id=uuid.uuid4())
    assert e.value.code == "ISSUE_NOT_FOUND"


#: (actor, issue placement, allowed). Placement: "stream" = linked to the Network stream; "app" = linked
#: to the Billing DR Application; "bare" = Event only. Creator is the executor; owner is the teammate.
AUTHORITY = [
    ("admin_id", "bare", True),
    ("coordinator_id", "bare", True),
    ("ws_lead_id", "stream", True),  # Lead of that stream
    ("ws_lead_id", "bare", True),  # WORK_STREAM_LEAD ✓ is unscoped (RBAC_MATRIX.md:23)
    ("system_owner_id", "app", True),  # owner slot of that Application
    ("system_owner_id", "stream", False),
    ("business_owner_id", "app", False),  # "comment/create if permitted" -> no grant
    ("manager_id", "bare", False),  # MANAGER ✓ is unscoped in the matrix; grant table reads it as True
    ("executor_id", "bare", True),  # the creator
    ("teammate_id", "bare", True),  # the owner
    ("outsider_id", "bare", True),  # EXECUTOR ✓ is unscoped too
    ("roleless_member_id", "bare", False),  # no role, not creator, not owner
]


@pytest.mark.parametrize(("actor", "placement", "allowed"), AUTHORITY)
async def test_handle_authority(
    session: AsyncSession, clock: FakeClock, actor: str, placement: str, allowed: bool
) -> None:
    w = await build_world(session)
    links: dict[str, Any] = {}
    if placement == "stream":
        links["work_stream_id"] = w.work_stream_id
    if placement == "app":
        links["dr_application_id"] = w.dr_application_id
    issue_id = await seed_issue(session, w, owner_id=w.teammate_id, created_by=w.executor_id, **links)

    if allowed or actor == "manager_id":
        # MANAGER holds CREATE_ISSUE_FINDING unscoped (RBAC_MATRIX.md:23 ✓), so handling follows.
        await _run(session, clock, "review", actor_id=getattr(w, actor), issue_id=issue_id)
        assert (await issue_row(session, issue_id)).status == IN_REVIEW
    else:
        with pytest.raises(AppError) as e:
            await _run(session, clock, "review", actor_id=getattr(w, actor), issue_id=issue_id)
        assert (e.value.code, e.value.status_code) == ("AUTHORIZATION_REQUIRED", 403)


async def test_an_open_issue_never_blocks_its_task(session: AsyncSession, clock: FakeClock) -> None:
    """D-253: an Issue linked to a Task changes neither readiness nor the Task's commands."""
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await create_issue(
        session, actor_id=w.executor_id, dr_event_id=w.event_id, item_type="RISK",
        title="Backup may be stale", description="Last snapshot older than RPO", task_id=task_id, clock=clock,
    )  # fmt: skip
    task = await get_task(session, task_id)
    assert task is not None

    assert (await readiness(session, task)).ready
    await TaskTransitionService.start(
        session, actor_id=w.executor_id, task_id=task_id, expected_version=1, clock=clock
    )

    assert (await task_row(session, task_id)).status == "IN_PROGRESS"
    assert (
        await session.execute(text("SELECT count(*) FROM blockers WHERE task_id = :t"), {"t": task_id})
    ).scalar_one() == 0


async def test_create_rejects_links_outside_the_event(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    from tests.factories import seed_foreign_task

    foreign_task, _ = await seed_foreign_task(session, w)
    with pytest.raises(AppError) as e:
        await create_issue(
            session, actor_id=w.coordinator_id, dr_event_id=w.event_id, item_type="ISSUE",
            title="x", description="y", task_id=foreign_task, clock=clock,
        )  # fmt: skip
    assert (e.value.code, e.value.details["field"]) == ("CROSS_EVENT_REFERENCE", "task_id")


async def test_create_writes_audit_and_outbox_and_requires_authority(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    issue = await create_issue(
        session, actor_id=w.executor_id, dr_event_id=w.event_id, item_type="LESSON",
        title="  Runbook step 4 is stale  ", description="d", work_stream_id=w.work_stream_id, clock=clock,
    )  # fmt: skip
    assert (issue.status, issue.version, issue.title) == (OPEN, 1, "Runbook step 4 is stale")
    assert await audit_actions(session, issue.id) == ["ISSUE_CREATED"]
    assert await outbox_types(session, issue.id) == ["IssueChanged"]

    with pytest.raises(AppError) as e:
        await create_issue(
            session, actor_id=w.business_owner_id, dr_event_id=w.event_id, item_type="ISSUE",
            title="x", description="y", clock=clock,
        )  # fmt: skip
    assert e.value.code == "AUTHORIZATION_REQUIRED"
    with pytest.raises(AppError) as e:
        await create_issue(
            session,
            actor_id=w.stranger_id,
            dr_event_id=w.event_id,
            item_type="ISSUE",
            title="x",
            description="y",
            clock=clock,
        )
    assert e.value.code == "DR_EVENT_NOT_FOUND"
