"""BUILD-06 session b: the Finish-to-Start dependency DAG, Milestone gates, derived Ready, and the
dependency-graph projection with blocked-path impact.

Invariant #2: HARD dependencies block Start; ADVISORY warn; no self edges, no directed cycles
(non-configurable). Ready = NOT_STARTED + HARD predecessors COMPLETED + HARD gating Milestones
ACHIEVED (STATE_MACHINES.md:50).
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.dr_events.models import DrEvent
from app.dr_events.participants import enrol_participant
from app.dr_events.readiness_service import evaluate_readiness
from app.dr_events.transition_service import DrEventTransitionService
from app.policies_admin.commands import set_policy_value
from app.tasks_dependencies.dependency_service import DependencyService
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.queries import Readiness, dependency_graph, event_graph_has_cycle, readiness
from app.tasks_dependencies.transition_service import TaskTransitionService
from app.work_streams.commands import get_or_create_work_stream
from tests.factories import (
    World,
    audit_actions,
    build_world,
    count,
    depend,
    grant_role,
    headers,
    http,
    login,
    make_application,
    make_user,
    outbox_types,
    seed_dr_application,
    seed_foreign_task,
    seed_gate,
    seed_milestone,
    seed_task,
    task_row,
)

pytestmark = [pytest.mark.domain]


async def add(
    session: AsyncSession,
    clock: FakeClock,
    *,
    actor_id: uuid.UUID,
    predecessor: uuid.UUID,
    successor: uuid.UUID,
    strength: str = "HARD",
) -> Any:
    return await DependencyService.add_task_dependency(
        session,
        actor_id=actor_id,
        predecessor_task_id=predecessor,
        successor_task_id=successor,
        strength=strength,
        clock=clock,
    )


async def ready_of(session: AsyncSession, task_id: uuid.UUID) -> Readiness:
    task = await session.get(Task, task_id)
    assert task is not None
    return await readiness(session, task)


async def live_edges(session: AsyncSession, w: World) -> int:
    return await count(
        session,
        "SELECT count(*) FROM task_dependencies WHERE dr_event_id = :e AND deleted_at IS NULL",
        e=w.event_id,
    )


# --------------------------------------------------------------------------------------------
# Task -> Task edges: creation, rejection, removal
# --------------------------------------------------------------------------------------------


async def test_add_creates_one_live_edge_with_audit_and_outbox_on_the_successor(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)

    edge = await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b, strength="ADVISORY")

    row = (
        await session.execute(
            text(
                "SELECT dr_event_id, predecessor_task_id, successor_task_id, strength, dependency_type "
                "FROM task_dependencies WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": edge.id},
        )
    ).one()
    assert (row.dr_event_id, row.predecessor_task_id, row.successor_task_id) == (w.event_id, a, b)
    assert (row.strength, row.dependency_type) == ("ADVISORY", "FINISH_TO_START")
    assert await audit_actions(session, edge.id) == ["TASK_DEPENDENCY_CREATED"]
    assert await outbox_types(session, b) == ["TaskChanged"]


async def test_self_dependency_is_rejected(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    a = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=a)

    assert (exc.value.code, exc.value.status_code) == ("SELF_DEPENDENCY", 422)
    assert await live_edges(session, w) == 0


@pytest.mark.parametrize("second_strength", ["HARD", "ADVISORY"])
async def test_a_second_live_edge_for_the_same_pair_is_rejected_whatever_its_strength(
    session: AsyncSession, clock: FakeClock, second_strength: str
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b, strength=second_strength)

    assert (exc.value.code, exc.value.status_code) == ("DEPENDENCY_EXISTS", 409)
    assert await live_edges(session, w) == 1


async def test_edge_across_events_is_rejected(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    a = await seed_task(session, w)
    _other_event, foreign = await seed_foreign_task(session, w)

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=w.admin_id, predecessor=foreign, successor=a)

    assert (exc.value.code, exc.value.status_code) == ("CROSS_EVENT_DEPENDENCY", 422)


async def test_two_node_cycle_is_rejected_with_its_path(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=w.admin_id, predecessor=b, successor=a)

    assert (exc.value.code, exc.value.status_code) == ("DEPENDENCY_CYCLE", 409)
    assert exc.value.details["cycle_path"] == [str(b), str(a), str(b)]


async def test_three_node_cycle_is_rejected_with_its_path(session: AsyncSession, clock: FakeClock) -> None:
    """The case BUILD-05's reverse-edge import guard could not see."""
    w = await build_world(session)
    a = await seed_task(session, w)
    b = await seed_task(session, w)
    c = await seed_task(session, w)
    await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)
    await add(session, clock, actor_id=w.admin_id, predecessor=b, successor=c)
    audit_before = Counter(await audit_actions(session, c))

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=w.admin_id, predecessor=c, successor=a)

    assert exc.value.code == "DEPENDENCY_CYCLE"
    assert exc.value.details["cycle_path"] == [str(c), str(a), str(b), str(c)]
    assert await live_edges(session, w) == 2
    assert Counter(await audit_actions(session, c)) == audit_before


async def test_an_advisory_edge_still_closes_a_cycle(session: AsyncSession, clock: FakeClock) -> None:
    """No directed cycles of any strength (DATA_MODEL.md:95): an ADVISORY cycle doesn't deadlock,
    but it's still an incoherent order."""
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b, strength="ADVISORY")

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=w.admin_id, predecessor=b, successor=a)

    assert exc.value.code == "DEPENDENCY_CYCLE"


async def test_a_removed_edge_no_longer_closes_a_cycle(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    edge = await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)
    await DependencyService.remove_task_dependency(
        session, actor_id=w.admin_id, dependency_id=edge.id, clock=clock
    )

    reverse = await add(session, clock, actor_id=w.admin_id, predecessor=b, successor=a)

    assert reverse.successor_task_id == a


async def test_remove_soft_deletes_audits_and_allows_recreating_the_pair(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    edge = await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)

    removed = await DependencyService.remove_task_dependency(
        session, actor_id=w.admin_id, dependency_id=edge.id, clock=clock
    )

    assert removed.deleted_at == clock.now()
    assert await live_edges(session, w) == 0
    assert Counter(await audit_actions(session, edge.id)) == Counter(
        {"TASK_DEPENDENCY_CREATED": 1, "TASK_DEPENDENCY_DELETED": 1}
    )
    assert Counter(await outbox_types(session, b))["TaskChanged"] == 2
    again = await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)
    assert again.id != edge.id


async def test_removing_an_already_removed_edge_is_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    edge = await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)
    await DependencyService.remove_task_dependency(
        session, actor_id=w.admin_id, dependency_id=edge.id, clock=clock
    )

    with pytest.raises(AppError) as exc:
        await DependencyService.remove_task_dependency(
            session, actor_id=w.admin_id, dependency_id=edge.id, clock=clock
        )

    assert (exc.value.code, exc.value.status_code) == ("TASK_DEPENDENCY_NOT_FOUND", 404)


# --------------------------------------------------------------------------------------------
# Who may change dependencies (CHANGE_DEPENDENCIES + dependency.edit_requires, plan Risk #12)
# --------------------------------------------------------------------------------------------


@pytest.mark.auth
async def test_tasks_the_actor_cannot_see_are_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=w.stranger_id, predecessor=a, successor=b)

    assert (exc.value.code, exc.value.status_code) == ("TASK_NOT_FOUND", 404)


@pytest.mark.auth
@pytest.mark.parametrize("who", ["admin_id", "ws_lead_id", "manager_id", "teammate_id", "executor_id"])
async def test_scoped_roles_may_change_the_successors_dependencies(
    session: AsyncSession, clock: FakeClock, who: str
) -> None:
    """SCOPED_ROLE (default): CHANGE_DEPENDENCIES in the successor's scope (WS Lead of its stream,
    Manager of its Owning Team) or the matrix Executor cell "Team per policy" (EXECUTOR role and a
    member of the successor's Owning Team)."""
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)

    edge = await add(session, clock, actor_id=getattr(w, who), predecessor=a, successor=b)

    assert edge.successor_task_id == b


@pytest.mark.auth
@pytest.mark.parametrize("who", ["outsider_id", "roleless_member_id", "business_owner_id"])
async def test_actors_without_authority_over_the_successor_get_403(
    session: AsyncSession, clock: FakeClock, who: str
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=getattr(w, who), predecessor=a, successor=b)

    assert exc.value.status_code == 403
    assert await live_edges(session, w) == 0


@pytest.mark.auth
async def test_authority_is_judged_on_the_successor_not_the_predecessor(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    mine = await seed_task(session, w, app_scoped=False)  # in the WS Lead's stream
    theirs = await seed_task(session, w, app_scoped=False, work_stream_id=w.other_work_stream_id)

    edge = await add(session, clock, actor_id=w.ws_lead_id, predecessor=theirs, successor=mine)
    assert edge.successor_task_id == mine

    with pytest.raises(AppError) as exc:
        await add(session, clock, actor_id=w.ws_lead_id, predecessor=mine, successor=theirs)
    assert exc.value.status_code == 403


@pytest.mark.auth
async def test_coordinator_only_policy_leaves_only_admin_and_coordinator(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    await set_policy_value(
        session,
        actor_id=w.admin_id,
        key="dependency.edit_requires",
        value="COORDINATOR_ONLY",
        scope_type="GLOBAL",
        clock=clock,
    )
    coordinator = await make_user(session, "Coordinator")
    await grant_role(session, coordinator, "DR_COORDINATOR")
    await enrol_participant(session, w.event_id, coordinator, "EXPLICIT", added_by_user_id=w.admin_id)
    a, b, c = await seed_task(session, w), await seed_task(session, w), await seed_task(session, w)

    for who in (w.ws_lead_id, w.manager_id, w.teammate_id):
        with pytest.raises(AppError) as exc:
            await add(session, clock, actor_id=who, predecessor=a, successor=b)
        assert exc.value.status_code == 403

    await add(session, clock, actor_id=coordinator, predecessor=a, successor=b)
    await add(session, clock, actor_id=w.admin_id, predecessor=b, successor=c)
    assert await live_edges(session, w) == 2


# --------------------------------------------------------------------------------------------
# Derived Ready
# --------------------------------------------------------------------------------------------

#: case -> (predecessor task status | None, predecessor strength, milestone status | None,
#:          gate strength, expected ready, expected blocking kinds, expected advisory kinds)
READY_CASES: dict[str, tuple[str | None, str, str | None, str, bool, set[str], set[str]]] = {
    "no dependencies": (None, "HARD", None, "HARD", True, set(), set()),
    "HARD predecessor in progress": ("IN_PROGRESS", "HARD", None, "HARD", False, {"TASK"}, set()),
    "HARD predecessor completed": ("COMPLETED", "HARD", None, "HARD", True, set(), set()),
    "HARD predecessor cancelled still blocks": ("CANCELLED", "HARD", None, "HARD", False, {"TASK"}, set()),
    "ADVISORY predecessor pending warns": ("NOT_STARTED", "ADVISORY", None, "HARD", True, set(), {"TASK"}),
    "HARD gate not started": (None, "HARD", "NOT_STARTED", "HARD", False, {"MILESTONE"}, set()),
    "HARD gate ready for confirmation": (
        None,
        "HARD",
        "READY_FOR_CONFIRMATION",
        "HARD",
        False,
        {"MILESTONE"},
        set(),
    ),
    "HARD gate achieved": (None, "HARD", "ACHIEVED", "HARD", True, set(), set()),
    "HARD gate missed still blocks": (None, "HARD", "MISSED", "HARD", False, {"MILESTONE"}, set()),
    "ADVISORY gate pending warns": (None, "HARD", "IN_PROGRESS", "ADVISORY", True, set(), {"MILESTONE"}),
}


@pytest.mark.parametrize("case", list(READY_CASES))
async def test_readiness_truth_table(session: AsyncSession, case: str) -> None:
    pred_status, pred_strength, ms_status, gate_strength, ready, blocking, advisory = READY_CASES[case]
    w = await build_world(session)
    task_id = await seed_task(session, w)
    if pred_status is not None:
        pred = await seed_task(session, w, status=pred_status)
        await depend(session, w, predecessor=pred, successor=task_id, strength=pred_strength)
    if ms_status is not None:
        milestone = await seed_milestone(session, w, status=ms_status)
        await seed_gate(session, w, milestone_id=milestone, task_id=task_id, strength=gate_strength)

    r = await ready_of(session, task_id)

    assert r.ready is ready
    assert {g.kind for g in r.blocking} == blocking
    assert {g.kind for g in r.advisory_pending} == advisory


@pytest.mark.parametrize(
    "status", ["IN_PROGRESS", "BLOCKED", "READY_FOR_VALIDATION", "COMPLETED", "CANCELLED"]
)
async def test_only_not_started_tasks_are_ever_ready(session: AsyncSession, status: str) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status=status)

    assert (await ready_of(session, task_id)).ready is False


async def test_removed_edges_and_gates_never_count(session: AsyncSession) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    pred = await seed_task(session, w, status="IN_PROGRESS")
    await depend(session, w, predecessor=pred, successor=task_id)
    milestone = await seed_milestone(session, w)
    await seed_gate(session, w, milestone_id=milestone, task_id=task_id)
    await session.execute(
        text("UPDATE task_dependencies SET deleted_at = now() WHERE successor_task_id = :t"), {"t": task_id}
    )
    await session.execute(
        text("UPDATE milestone_dependencies SET deleted_at = now() WHERE successor_task_id = :t"),
        {"t": task_id},
    )

    assert (await ready_of(session, task_id)).ready is True


async def test_start_is_blocked_by_a_hard_milestone_gate(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    milestone = await seed_milestone(session, w)
    await seed_gate(session, w, milestone_id=milestone, task_id=task_id)

    with pytest.raises(AppError) as exc:
        await TaskTransitionService.start(
            session, actor_id=w.admin_id, task_id=task_id, expected_version=1, clock=clock
        )

    assert exc.value.code == "DEPENDENCY_NOT_SATISFIED"
    assert exc.value.details == {"blocking_task_ids": [], "blocking_milestone_ids": [str(milestone)]}
    assert (await task_row(session, task_id)).status == "NOT_STARTED"


async def test_a_milestone_gate_can_be_overridden_with_a_reason(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    milestone = await seed_milestone(session, w)
    await seed_gate(session, w, milestone_id=milestone, task_id=task_id)

    task = await TaskTransitionService.start(
        session,
        actor_id=w.admin_id,
        task_id=task_id,
        expected_version=1,
        override_reason="Gate held by phone",
        clock=clock,
    )

    assert task.status == "IN_PROGRESS"
    metadata = await session.scalar(
        text("SELECT metadata FROM overrides WHERE target_id = :t"), {"t": task_id}
    )
    assert metadata["blocking_milestone_ids"] == [str(milestone)]


async def test_starting_over_pending_advisory_work_records_the_warning(
    session: AsyncSession, clock: FakeClock
) -> None:
    """Invariant #2 "ADVISORY warn": start succeeds, and the pending ADVISORY predecessor is written
    into the TASK_STARTED audit row (plan Risk #16)."""
    w = await build_world(session)
    task_id = await seed_task(session, w)
    pred = await seed_task(session, w)
    await depend(session, w, predecessor=pred, successor=task_id, strength="ADVISORY")

    task = await TaskTransitionService.start(
        session, actor_id=w.admin_id, task_id=task_id, expected_version=1, clock=clock
    )

    assert task.status == "IN_PROGRESS"
    after = await session.scalar(
        text("SELECT after_data FROM audit_events WHERE entity_id = :t AND action = 'TASK_STARTED'"),
        {"t": task_id},
    )
    assert after["advisory_pending_task_ids"] == [str(pred)]
    assert after["advisory_pending_milestone_ids"] == []


# --------------------------------------------------------------------------------------------
# Milestone gates through the service (no HTTP route until BUILD-07, plan Risk #13)
# --------------------------------------------------------------------------------------------


async def test_a_hard_gate_holds_the_task_until_the_milestone_is_achieved(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    milestone = await seed_milestone(session, w)

    gate = await DependencyService.add_milestone_gate(
        session, actor_id=w.admin_id, milestone_id=milestone, successor_task_id=task_id, clock=clock
    )

    assert (gate.milestone_id, gate.successor_task_id, gate.strength) == (milestone, task_id, "HARD")
    assert (await ready_of(session, task_id)).ready is False
    assert "MILESTONE_GATE_ADDED" in await audit_actions(session, task_id)
    assert await outbox_types(session, task_id) == ["TaskChanged"]
    await session.execute(text("UPDATE milestones SET status = 'ACHIEVED' WHERE id = :m"), {"m": milestone})
    assert (await ready_of(session, task_id)).ready is True


async def test_duplicate_gate_is_rejected(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    milestone = await seed_milestone(session, w)
    await DependencyService.add_milestone_gate(
        session, actor_id=w.admin_id, milestone_id=milestone, successor_task_id=task_id, clock=clock
    )

    with pytest.raises(AppError) as exc:
        await DependencyService.add_milestone_gate(
            session, actor_id=w.admin_id, milestone_id=milestone, successor_task_id=task_id, clock=clock
        )

    assert (exc.value.code, exc.value.status_code) == ("DEPENDENCY_EXISTS", 409)


async def test_a_milestone_in_another_event_cannot_gate_this_task(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    other_event, _ = await seed_foreign_task(session, w)
    other_ws = await session.scalar(
        text("SELECT id FROM work_streams WHERE dr_event_id = :e"), {"e": other_event}
    )
    milestone = await seed_milestone(session, w, dr_event_id=other_event, work_stream_id=other_ws)

    with pytest.raises(AppError) as exc:
        await DependencyService.add_milestone_gate(
            session, actor_id=w.admin_id, milestone_id=milestone, successor_task_id=task_id, clock=clock
        )
    assert (exc.value.code, exc.value.status_code) == ("CROSS_EVENT_DEPENDENCY", 422)

    # ...and an actor who can't see that other Event learns nothing about the Milestone.
    with pytest.raises(AppError) as hidden:
        await DependencyService.add_milestone_gate(
            session, actor_id=w.ws_lead_id, milestone_id=milestone, successor_task_id=task_id, clock=clock
        )
    assert (hidden.value.code, hidden.value.status_code) == ("MILESTONE_NOT_FOUND", 404)


async def test_unknown_milestone_is_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await DependencyService.add_milestone_gate(
            session, actor_id=w.admin_id, milestone_id=uuid.uuid4(), successor_task_id=task_id, clock=clock
        )

    assert exc.value.code == "MILESTONE_NOT_FOUND"


async def test_removing_a_gate_releases_the_task(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    milestone = await seed_milestone(session, w)
    gate = await DependencyService.add_milestone_gate(
        session, actor_id=w.admin_id, milestone_id=milestone, successor_task_id=task_id, clock=clock
    )

    await DependencyService.remove_milestone_gate(
        session, actor_id=w.admin_id, milestone_dependency_id=gate.id, clock=clock
    )

    assert (await ready_of(session, task_id)).ready is True
    assert "MILESTONE_GATE_REMOVED" in await audit_actions(session, task_id)


# --------------------------------------------------------------------------------------------
# Graph projection with blocked-path impact (UI_UX.md:191-196)
# --------------------------------------------------------------------------------------------


async def test_dependency_graph_projection(session: AsyncSession) -> None:
    """A is BLOCKED. HARD: A->B->C, A->E(COMPLETED)->F, A->G(CANCELLED)->H. ADVISORY: A->D.
    Milestone M (not achieved) gates C. B runs on a second DR Application."""
    w = await build_world(session)
    other_dr_app = await seed_dr_application(
        session, w, application_id=await make_application(session, "Ledger")
    )
    a = await seed_task(session, w, status="BLOCKED")
    b = await seed_task(session, w, dr_application_id=other_dr_app)
    c = await seed_task(session, w, app_scoped=False)
    d = await seed_task(session, w)
    e = await seed_task(session, w, status="COMPLETED")
    f = await seed_task(session, w)
    g = await seed_task(session, w, status="CANCELLED")
    h = await seed_task(session, w)
    for pred, succ in ((a, b), (b, c), (a, e), (e, f), (a, g), (g, h)):
        await depend(session, w, predecessor=pred, successor=succ)
    await depend(session, w, predecessor=a, successor=d, strength="ADVISORY")
    m = await seed_milestone(session, w)
    await seed_gate(session, w, milestone_id=m, task_id=c)

    graph = await dependency_graph(session, w.event_id)

    nodes = {n.id: n for n in graph.nodes}
    assert set(nodes) == {a, b, c, d, e, f, g, h, m}
    assert (nodes[m].kind, nodes[m].ready, nodes[a].kind) == ("MILESTONE", None, "TASK")
    assert nodes[a].active_blocker_count == 1
    assert (nodes[b].ready, set(nodes[b].blocked_by)) == (False, {a})
    assert set(nodes[c].blocked_by) == {b, m}
    assert (nodes[d].ready, nodes[d].blocked_by, nodes[d].advisory_pending) == (True, [], [a])
    assert (nodes[f].ready, nodes[f].blocked_by) == (True, [])  # E is COMPLETED
    assert set(nodes[h].blocked_by) == {g}  # a CANCELLED predecessor still blocks

    edges = {(x.from_id, x.to_id): x for x in graph.edges}
    assert (edges[(a, d)].strength, edges[(a, d)].kind) == ("ADVISORY", "TASK_DEPENDENCY")
    assert (edges[(m, c)].kind, edges[(m, c)].strength) == ("MILESTONE_GATE", "HARD")

    [path] = graph.blocked_paths
    assert path.root_task_id == a
    # Not D (ADVISORY), not E (COMPLETED) or F behind it, not G (CANCELLED) or H behind it.
    assert set(path.downstream_task_ids) == {b, c}
    assert set(path.impacted_dr_application_ids) == {w.dr_application_id, other_dr_app}


async def test_graph_without_blocked_tasks_has_no_blocked_paths(session: AsyncSession) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w, status="IN_PROGRESS"), await seed_task(session, w)
    await depend(session, w, predecessor=a, successor=b)

    graph = await dependency_graph(session, w.event_id)

    assert graph.blocked_paths == []
    assert len(graph.edges) == 1


# --------------------------------------------------------------------------------------------
# Acyclicity outside the API: legacy rows, import, and D-224 readiness
# --------------------------------------------------------------------------------------------


async def test_event_graph_cycle_detection(session: AsyncSession) -> None:
    w = await build_world(session)
    a, b, c = await seed_task(session, w), await seed_task(session, w), await seed_task(session, w)
    await depend(session, w, predecessor=a, successor=b)
    await depend(session, w, predecessor=b, successor=c)
    assert await event_graph_has_cycle(session, w.event_id) is False

    await depend(session, w, predecessor=c, successor=a)  # raw SQL: bypasses the service on purpose
    assert await event_graph_has_cycle(session, w.event_id) is True

    await session.execute(
        text("UPDATE task_dependencies SET deleted_at = now() WHERE predecessor_task_id = :c"), {"c": c}
    )
    assert await event_graph_has_cycle(session, w.event_id) is False


async def _planned_event(session: AsyncSession, w: World) -> uuid.UUID:
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dr_events "
            "(id, name, event_type, status, version, created_by_user_id, event_timezone) "
            "VALUES (:id, 'Planned', 'PLANNED_DR', 'PLANNED', 1, :u, 'UTC')"
        ),
        {"id": event_id, "u": w.admin_id},
    )
    await session.flush()
    return event_id


async def test_a_cycle_blocks_activation_even_with_an_override_reason(
    session: AsyncSession, clock: FakeClock
) -> None:
    """D-224: dependency_graph_acyclic is HARD_STOP and not configurable; a cycle is invalid outright
    (FROZEN_DECISIONS.md §7.9), so no override reason gets past it (plan Risk #14)."""
    w = await build_world(session)
    event_id = await _planned_event(session, w)
    ws_id = (
        await get_or_create_work_stream(session, dr_event_id=event_id, name="Net", actor_id=w.admin_id)
    ).id
    ids = []
    for _ in range(2):
        tid = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO tasks "
                "(id, dr_event_id, work_stream_id, title, phase, owning_team_id, created_by_user_id) "
                "VALUES (:id, :e, :ws, 'T', 'FAILOVER', :team, :u)"
            ),
            {"id": tid, "e": event_id, "ws": ws_id, "team": w.team_id, "u": w.admin_id},
        )
        ids.append(tid)
    for pred, succ in ((ids[0], ids[1]), (ids[1], ids[0])):
        await session.execute(
            text(
                "INSERT INTO task_dependencies "
                "(id, dr_event_id, predecessor_task_id, successor_task_id, created_by_user_id) "
                "VALUES (:id, :e, :p, :s, :u)"
            ),
            {"id": uuid.uuid4(), "e": event_id, "p": pred, "s": succ, "u": w.admin_id},
        )
    await session.flush()

    with pytest.raises(AppError) as exc:
        await DrEventTransitionService.activate(
            session,
            actor_id=w.admin_id,
            event_id=event_id,
            expected_version=1,
            override_reason="Override everything",
            clock=clock,
        )

    assert exc.value.code == "READINESS_HARD_STOP"
    assert exc.value.details["failed_keys"] == ["readiness.dependency_graph_acyclic"]
    assert await count(session, "SELECT count(*) FROM overrides WHERE dr_event_id = :e", e=event_id) == 0


async def test_an_acyclic_graph_satisfies_the_readiness_key(session: AsyncSession) -> None:
    w = await build_world(session)
    event_id = await _planned_event(session, w)
    event = await session.get(DrEvent, event_id)
    assert event is not None

    results = {r.key: r for r in await evaluate_readiness(session, event)}

    assert results["readiness.dependency_graph_acyclic"].satisfied is True
    assert results["readiness.dependency_graph_acyclic"].severity == "HARD_STOP"


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------


@pytest.mark.api
async def test_post_task_dependency_requires_an_idempotency_key(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    sid, csrf = await login(session, redis_client, clock, w.admin_id)

    async with http(session, redis_client, clock) as c:
        r = await c.post(
            "/api/v1/task-dependencies",
            cookies={"drcc_session": sid},
            headers={"X-CSRF-Token": csrf},
            json={"predecessor_task_id": str(a), "successor_task_id": str(b)},
        )

    assert (r.status_code, r.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")
    assert await live_edges(session, w) == 0
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_post_replay_returns_the_original_201_and_creates_one_edge(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    sid, csrf = await login(session, redis_client, clock, w.ws_lead_id)
    same = headers(csrf)
    body = {"predecessor_task_id": str(a), "successor_task_id": str(b), "strength": "HARD"}

    async with http(session, redis_client, clock) as c:
        first = await c.post(
            "/api/v1/task-dependencies", cookies={"drcc_session": sid}, headers=same, json=body
        )
        replay = await c.post(
            "/api/v1/task-dependencies", cookies={"drcc_session": sid}, headers=same, json=body
        )

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert (first.json()["predecessor_task_id"], first.json()["strength"]) == (str(a), "HARD")
    assert await live_edges(session, w) == 1
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_delete_requires_a_key_and_its_replay_returns_the_original(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    edge = await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)
    sid, csrf = await login(session, redis_client, clock, w.admin_id)
    same = headers(csrf)
    url = f"/api/v1/task-dependencies/{edge.id}"

    async with http(session, redis_client, clock) as c:
        keyless = await c.delete(url, cookies={"drcc_session": sid}, headers={"X-CSRF-Token": csrf})
        first = await c.delete(url, cookies={"drcc_session": sid}, headers=same)
        replay = await c.delete(url, cookies={"drcc_session": sid}, headers=same)

    assert (keyless.status_code, keyless.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["deleted_at"] is not None
    assert Counter(await audit_actions(session, edge.id))["TASK_DEPENDENCY_DELETED"] == 1
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
async def test_cycle_over_http_explains_itself(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    """UI_UX.md:195: a cycle-creating edit is rejected with a clear explanation."""
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    await add(session, clock, actor_id=w.admin_id, predecessor=a, successor=b)
    sid, csrf = await login(session, redis_client, clock, w.admin_id)

    async with http(session, redis_client, clock) as c:
        r = await c.post(
            "/api/v1/task-dependencies",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"predecessor_task_id": str(b), "successor_task_id": str(a)},
        )

    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "DEPENDENCY_CYCLE"
    assert error["details"]["cycle_path"] == [str(b), str(a), str(b)]
    assert "cycle" in error["message"].lower()
    await redis_client.delete(f"drcc:session:{sid}")


@pytest.mark.api
@pytest.mark.auth
async def test_unauthenticated_dependency_command_is_401(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)

    async with http(session, redis_client, clock) as c:
        r = await c.post(
            "/api/v1/task-dependencies",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"predecessor_task_id": str(a), "successor_task_id": str(b)},
        )

    assert r.status_code == 401


@pytest.mark.api
async def test_get_dependency_graph_over_http(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    a = await seed_task(session, w, status="BLOCKED")
    b = await seed_task(session, w)
    await depend(session, w, predecessor=a, successor=b)
    member_sid, _ = await login(session, redis_client, clock, w.executor_id)
    stranger_sid, _ = await login(session, redis_client, clock, w.stranger_id)

    async with http(session, redis_client, clock) as c:
        seen = await c.get(
            f"/api/v1/dr-events/{w.event_id}/dependency-graph", cookies={"drcc_session": member_sid}
        )
        hidden = await c.get(
            f"/api/v1/dr-events/{w.event_id}/dependency-graph", cookies={"drcc_session": stranger_sid}
        )

    assert seen.status_code == 200
    body = seen.json()
    assert body["dr_event_id"] == str(w.event_id)
    assert {n["id"] for n in body["nodes"]} == {str(a), str(b)}
    assert body["edges"] == [
        {
            "id": body["edges"][0]["id"],
            "kind": "TASK_DEPENDENCY",
            "from_id": str(a),
            "to_id": str(b),
            "strength": "HARD",
        }
    ]
    assert body["blocked_paths"][0]["root_task_id"] == str(a)
    assert body["blocked_paths"][0]["downstream_task_ids"] == [str(b)]
    assert hidden.status_code == 404
    for sid in (member_sid, stranger_sid):
        await redis_client.delete(f"drcc:session:{sid}")


# --------------------------------------------------------------------------------------------
# Concurrency: every edge write holds the Event's dependency-graph lock (plan Risk #11)
# --------------------------------------------------------------------------------------------


async def _lock_is_free_elsewhere(engine: AsyncEngine, dr_event_id: uuid.UUID) -> bool:
    """Ask from a *second* connection whether the Event's graph lock could be taken right now."""
    async with engine.connect() as other:
        return bool(
            await other.scalar(
                select(
                    func.pg_try_advisory_xact_lock(
                        func.hashtextextended(f"dependency_graph:{dr_event_id}", 0)
                    )
                )
            )
        )


@pytest.mark.parametrize("write", ["add_edge", "remove_edge", "add_gate", "remove_gate"])
async def test_every_dependency_write_holds_the_event_graph_lock_until_commit(
    session: AsyncSession, engine: AsyncEngine, clock: FakeClock, write: str
) -> None:
    """Two concurrent inserts A->B and B->A are each acyclic alone; only the lock stops both committing.
    A second connection can't take the lock while this transaction holds it, so it would wait."""
    w = await build_world(session)
    a, b = await seed_task(session, w), await seed_task(session, w)
    milestone = await seed_milestone(session, w)
    # Raw-SQL rows for the remove cases: seeding through the service would itself take the lock, and
    # the fixture's `commit()` only releases a SAVEPOINT -- the lock would still be held.
    edge_id = await depend(session, w, predecessor=a, successor=b)
    gate_id = await seed_gate(session, w, milestone_id=milestone, task_id=b)
    assert await _lock_is_free_elsewhere(engine, w.event_id) is True

    if write == "add_edge":
        c = await seed_task(session, w)
        await add(session, clock, actor_id=w.admin_id, predecessor=b, successor=c)
    elif write == "remove_edge":
        await DependencyService.remove_task_dependency(
            session, actor_id=w.admin_id, dependency_id=edge_id, clock=clock
        )
    elif write == "add_gate":
        await DependencyService.add_milestone_gate(
            session, actor_id=w.admin_id, milestone_id=milestone, successor_task_id=a, clock=clock
        )
    else:
        await DependencyService.remove_milestone_gate(
            session, actor_id=w.admin_id, milestone_dependency_id=gate_id, clock=clock
        )

    assert await _lock_is_free_elsewhere(engine, w.event_id) is False
