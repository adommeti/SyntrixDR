"""API negative suite for every command BUILD-06 adds or changes, over HTTP (BUILD-06 session d).

Each command is one `Spec`: how to set up a state where it would succeed, the request, who may and may
not call it, and its own guard and conflict cases. Each negative category is one parametrized test
across all specs:

    401 unauthenticated · 403 forbidden (visible, no authority) · 404 invisible/foreign
    the command's guard code · 409 conflicts · idempotent replay · 422 malformed request

Every negative also asserts nothing was written (the Event's audit and outbox rows are unchanged) and,
for domain errors, the API_CONTRACT envelope (`error.code`, `error.message`, `error.correlation_id`).
Schema-level 422s are FastAPI's own `detail` body -- the app has no RequestValidationError handler.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.work_streams.commands import create_work_stream
from tests.factories import World, build_world, count, depend, headers, http, login, seed_task

pytestmark = [pytest.mark.api]

Ids = dict[str, uuid.UUID]
Setup = Callable[[AsyncSession, World, FakeClock], Awaitable[Ids]]
Mutate = Callable[[AsyncSession, World, Ids, FakeClock], Awaitable[None]]
PathFn = Callable[[World, Ids], str]
BodyFn = Callable[[World, Ids], dict[str, Any] | None]


@dataclass(frozen=True)
class Case:
    """A negative: optional state change, optional body/path override, and the expected outcome."""

    status: int
    code: str
    mutate: Mutate | None = None
    body: BodyFn | None = None
    path: PathFn | None = None


@dataclass(frozen=True)
class Spec:
    method: str
    path: PathFn
    body: BodyFn
    setup: Setup
    ok_actor: str
    ok_status: int
    forbidden_actor: str
    not_found: tuple[str, Case]  # (actor, case) -- the actor who can't see it, or a missing object
    malformed: Case  # status 422; code unused (schema errors carry FastAPI's own body)
    guard: Case | None = None
    conflicts: dict[str, Case] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------
# Setups and state changes
# --------------------------------------------------------------------------------------------


def task_in(status: str, **seed: Any) -> Setup:
    async def setup(session: AsyncSession, w: World, clock: FakeClock) -> Ids:
        return {"task": await seed_task(session, w, status=status, **seed)}

    return setup


def sql(statement: str) -> Mutate:
    """Run one statement with :t (the Task), :e (the Event) and :u (the System Owner) bound."""

    async def mutate(session: AsyncSession, w: World, ids: Ids, clock: FakeClock) -> None:
        await session.execute(
            text(statement), {"t": ids.get("task"), "e": w.event_id, "u": w.system_owner_id}
        )

    return mutate


async def add_open_predecessor(session: AsyncSession, w: World, ids: Ids, clock: FakeClock) -> None:
    pred = await seed_task(session, w, status="IN_PROGRESS")
    await depend(session, w, predecessor=pred, successor=ids["task"])


async def two_tasks(session: AsyncSession, w: World, clock: FakeClock) -> Ids:
    return {"a": await seed_task(session, w), "b": await seed_task(session, w)}


async def an_edge(session: AsyncSession, w: World, clock: FakeClock) -> Ids:
    a, b = await seed_task(session, w), await seed_task(session, w)
    return {"a": a, "b": b, "edge": await depend(session, w, predecessor=a, successor=b)}


async def reverse_edge(session: AsyncSession, w: World, ids: Ids, clock: FakeClock) -> None:
    await depend(session, w, predecessor=ids["b"], successor=ids["a"])


async def same_edge(session: AsyncSession, w: World, ids: Ids, clock: FakeClock) -> None:
    await depend(session, w, predecessor=ids["a"], successor=ids["b"])


async def nothing(session: AsyncSession, w: World, clock: FakeClock) -> Ids:
    return {}


async def closable_event(session: AsyncSession, w: World, clock: FakeClock) -> Ids:
    await session.execute(
        text("UPDATE dr_events SET status = 'FAILBACK_IN_PROGRESS' WHERE id = :e"), {"e": w.event_id}
    )
    return {}


async def open_monitoring_task(session: AsyncSession, w: World, ids: Ids, clock: FakeClock) -> None:
    ws = await create_work_stream(
        session,
        actor_id=w.admin_id,
        dr_event_id=w.event_id,
        name="Monitoring",
        stream_type="MONITORING",
        clock=clock,
    )
    ws_id = ws.id
    await seed_task(session, w, status="IN_PROGRESS", app_scoped=False, work_stream_id=ws_id)


async def existing_stream_named_x(session: AsyncSession, w: World, ids: Ids, clock: FakeClock) -> None:
    await create_work_stream(session, actor_id=w.admin_id, dr_event_id=w.event_id, name="X", clock=clock)


def task_path(command: str) -> PathFn:
    return lambda w, ids: f"/api/v1/tasks/{ids['task']}/{command}"


def body(**fields: Any) -> BodyFn:
    return lambda w, ids: dict(fields)


def stale(**fields: Any) -> Case:
    return Case(409, "CONCURRENCY_CONFLICT", body=body(expected_version=9, **fields))


def illegal_from(status: str, **fields: Any) -> Case:
    return Case(
        409,
        "INVALID_TRANSITION",
        mutate=sql(f"UPDATE tasks SET status = '{status}' WHERE id = :t"),
        body=body(expected_version=1, **fields),
    )


TASK_404 = ("stranger_id", Case(404, "TASK_NOT_FOUND"))
MISSING_VERSION = Case(422, "", body=body())


# --------------------------------------------------------------------------------------------
# The commands
# --------------------------------------------------------------------------------------------

SPECS: dict[str, Spec] = {
    "start": Spec(
        "POST",
        task_path("start"),
        body(expected_version=1),
        task_in("NOT_STARTED"),
        ok_actor="executor_id",
        ok_status=200,
        forbidden_actor="outsider_id",
        not_found=TASK_404,
        malformed=MISSING_VERSION,
        guard=Case(409, "DEPENDENCY_NOT_SATISFIED", mutate=add_open_predecessor),
        conflicts={"stale version": stale(), "illegal transition": illegal_from("COMPLETED")},
    ),
    "block": Spec(
        "POST",
        task_path("block"),
        body(expected_version=1, reason="Waiting on DNS"),
        task_in("IN_PROGRESS"),
        ok_actor="executor_id",
        ok_status=200,
        forbidden_actor="outsider_id",
        not_found=TASK_404,
        malformed=MISSING_VERSION,
        guard=Case(400, "OVERRIDE_REASON_REQUIRED", body=body(expected_version=1, reason="  ")),
        conflicts={
            "stale version": stale(reason="x"),
            "illegal transition": illegal_from("NOT_STARTED", reason="x"),
        },
    ),
    "resume": Spec(
        "POST",
        task_path("resume"),
        body(expected_version=1),
        task_in("BLOCKED", active_blocker=False),
        ok_actor="executor_id",
        ok_status=200,
        forbidden_actor="outsider_id",
        not_found=TASK_404,
        malformed=MISSING_VERSION,
        guard=Case(
            400,
            "OVERRIDE_REASON_REQUIRED",
            mutate=sql("UPDATE blockers SET status = 'OPEN' WHERE task_id = :t"),
        ),
        conflicts={"stale version": stale(), "illegal transition": illegal_from("IN_PROGRESS")},
    ),
    "submit-validation": Spec(
        "POST",
        task_path("submit-validation"),
        body(expected_version=1, verification_note="Checked"),
        task_in("IN_PROGRESS"),
        ok_actor="executor_id",
        ok_status=200,
        forbidden_actor="outsider_id",
        not_found=TASK_404,
        malformed=MISSING_VERSION,
        guard=Case(
            409, "EVIDENCE_REQUIRED", mutate=sql("UPDATE tasks SET evidence_required = TRUE WHERE id = :t")
        ),
        conflicts={"stale version": stale(), "illegal transition": illegal_from("BLOCKED")},
    ),
    "validate": Spec(
        "POST",
        task_path("validate"),
        body(expected_version=1, approve=True),
        task_in("READY_FOR_VALIDATION"),
        ok_actor="system_owner_id",
        ok_status=200,
        forbidden_actor="business_owner_id",
        not_found=TASK_404,
        malformed=Case(422, "", body=body(expected_version=1)),  # approve is required
        guard=Case(
            403,
            "SELF_VALIDATION_FORBIDDEN",
            mutate=sql("UPDATE tasks SET current_assignee_user_id = :u WHERE id = :t"),
        ),
        conflicts={
            "stale version": stale(approve=True),
            "illegal transition": illegal_from("IN_PROGRESS", approve=True),
        },
    ),
    "cancel": Spec(
        "POST",
        task_path("cancel"),
        body(expected_version=1, reason="Descoped"),
        task_in("NOT_STARTED"),
        ok_actor="coordinator_id",
        ok_status=200,
        forbidden_actor="executor_id",
        not_found=TASK_404,
        malformed=MISSING_VERSION,
        guard=Case(400, "OVERRIDE_REASON_REQUIRED", body=body(expected_version=1, reason="")),
        conflicts={
            "stale version": stale(reason="x"),
            "illegal transition": illegal_from("COMPLETED", reason="x"),
        },
    ),
    "update task": Spec(
        "PATCH",
        lambda w, ids: f"/api/v1/tasks/{ids['task']}",
        body(expected_version=1, title="Renamed"),
        task_in("NOT_STARTED"),
        ok_actor="coordinator_id",
        ok_status=200,
        forbidden_actor="outsider_id",
        not_found=TASK_404,
        malformed=Case(422, "", body=body(expected_version=1, status="COMPLETED")),
        guard=Case(
            409,
            "TASK_METADATA_LOCKED",
            mutate=sql("UPDATE tasks SET status = 'COMPLETED' WHERE id = :t"),
        ),
        conflicts={"stale version": stale(title="Renamed")},
    ),
    "create task": Spec(
        "POST",
        lambda w, ids: f"/api/v1/dr-events/{w.event_id}/tasks",
        lambda w, ids: {
            "title": "New",
            "phase": "FAILOVER",
            "owning_team_id": str(w.team_id),
            "work_stream_id": str(w.work_stream_id),
        },
        nothing,
        ok_actor="coordinator_id",
        ok_status=201,
        forbidden_actor="executor_id",
        not_found=("stranger_id", Case(404, "DR_EVENT_NOT_FOUND")),
        malformed=Case(
            422,
            "",
            body=lambda w, ids: {
                "title": "New",
                "phase": "FAILOVER",
                "owning_team_id": str(w.team_id),
                "work_stream_id": str(w.work_stream_id),
                "evidence_min_count": -1,
            },
        ),
        guard=Case(
            422,
            "TASK_CONTEXT_REQUIRED",
            body=lambda w, ids: {"title": "New", "phase": "FAILOVER", "owning_team_id": str(w.team_id)},
        ),
    ),
    "add dependency": Spec(
        "POST",
        lambda w, ids: "/api/v1/task-dependencies",
        lambda w, ids: {"predecessor_task_id": str(ids["a"]), "successor_task_id": str(ids["b"])},
        two_tasks,
        ok_actor="ws_lead_id",
        ok_status=201,
        forbidden_actor="outsider_id",
        not_found=TASK_404,
        malformed=Case(
            422,
            "",
            body=lambda w, ids: {
                "predecessor_task_id": str(ids["a"]),
                "successor_task_id": str(ids["b"]),
                "strength": "SOFT",
            },
        ),
        guard=Case(409, "DEPENDENCY_CYCLE", mutate=reverse_edge),
        conflicts={"duplicate edge": Case(409, "DEPENDENCY_EXISTS", mutate=same_edge)},
    ),
    "remove dependency": Spec(
        "DELETE",
        lambda w, ids: f"/api/v1/task-dependencies/{ids['edge']}",
        lambda w, ids: None,
        an_edge,
        ok_actor="ws_lead_id",
        ok_status=200,
        forbidden_actor="outsider_id",
        not_found=("stranger_id", Case(404, "TASK_DEPENDENCY_NOT_FOUND")),
        malformed=Case(422, "", path=lambda w, ids: "/api/v1/task-dependencies/not-a-uuid"),
    ),
    "create work stream": Spec(
        "POST",
        lambda w, ids: f"/api/v1/dr-events/{w.event_id}/work-streams",
        body(name="X"),
        nothing,
        ok_actor="coordinator_id",
        ok_status=201,
        forbidden_actor="ws_lead_id",
        not_found=("stranger_id", Case(404, "DR_EVENT_NOT_FOUND")),
        malformed=Case(422, "", body=body(name="X", stream_type="LOGISTICS")),
        guard=Case(
            404, "USER_NOT_FOUND", body=lambda w, ids: {"name": "X", "lead_user_id": str(uuid.uuid4())}
        ),
        conflicts={"duplicate name": Case(409, "WORK_STREAM_EXISTS", mutate=existing_stream_named_x)},
    ),
    "close event": Spec(
        "POST",
        lambda w, ids: f"/api/v1/dr-events/{w.event_id}/close",
        body(expected_version=1),
        closable_event,
        ok_actor="coordinator_id",
        ok_status=200,
        forbidden_actor="executor_id",
        # EVENT_LIFECYCLE_COMMAND is checked before the Event is loaded (dr-events rule), so an outsider
        # gets 403, never 404; a 404 is a Coordinator naming an Event that doesn't exist.
        not_found=(
            "coordinator_id",
            Case(404, "DR_EVENT_NOT_FOUND", path=lambda w, ids: f"/api/v1/dr-events/{uuid.uuid4()}/close"),
        ),
        malformed=MISSING_VERSION,
        guard=Case(409, "MONITORING_CLOSURE_WARNING", mutate=open_monitoring_task),
        conflicts={
            "stale version": stale(),
            "illegal transition": Case(
                409,
                "INVALID_TRANSITION",
                mutate=sql("UPDATE dr_events SET status = 'ACTIVE' WHERE id = :e"),
                body=body(expected_version=1),
            ),
        },
    ),
}


# --------------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------------


async def written(session: AsyncSession, w: World) -> tuple[int, int]:
    return (
        await count(session, "SELECT count(*) FROM audit_events WHERE dr_event_id = :e", e=w.event_id),
        await count(session, "SELECT count(*) FROM outbox_events WHERE dr_event_id = :e", e=w.event_id),
    )


async def send(
    session: AsyncSession,
    redis_client: Redis,
    clock: FakeClock,
    w: World,
    spec: Spec,
    ids: Ids,
    *,
    actor: str | None,
    case: Case | None = None,
    key: str | None = None,
) -> Response:
    cookies: dict[str, str] = {}
    hdrs: dict[str, str] = {"Idempotency-Key": key or str(uuid.uuid4())}
    sid = None
    if actor is not None:
        sid, csrf = await login(session, redis_client, clock, getattr(w, actor))
        cookies = {"drcc_session": sid}
        hdrs = headers(csrf, key)
    path = (case.path if case and case.path else spec.path)(w, ids)
    payload = (case.body if case and case.body else spec.body)(w, ids)
    async with http(session, redis_client, clock) as c:
        r = await c.request(spec.method, path, cookies=cookies, headers=hdrs, json=payload)
    if sid:
        await redis_client.delete(f"drcc:session:{sid}")
    return r


def assert_envelope(r: Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    error = r.json()["error"]
    assert error["code"] == code
    assert error["message"] and error["correlation_id"]
    assert "Traceback" not in r.text


async def run_negative(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, spec: Spec, *, actor: str | None, case: Case
) -> None:
    w = await build_world(session)
    ids = await spec.setup(session, w, clock)
    if case.mutate is not None:
        await case.mutate(session, w, ids, clock)
    before = await written(session, w)

    r = await send(session, redis_client, clock, w, spec, ids, actor=actor, case=case)

    if case.status == 422 and not case.code:
        assert r.status_code == 422, r.text
        assert "detail" in r.json()
    else:
        assert_envelope(r, case.status, case.code)
    assert await written(session, w) == before, "a rejected command must write nothing"


def _params(extract: Callable[[Spec], list[tuple[str, Case]]]) -> list[Any]:
    return [
        pytest.param(name, label, case, id=f"{name}-{label}")
        for name, spec in SPECS.items()
        for label, case in extract(spec)
    ]


# --------------------------------------------------------------------------------------------
# The categories
# --------------------------------------------------------------------------------------------


@pytest.mark.auth
@pytest.mark.parametrize("name", list(SPECS))
async def test_unauthenticated_is_401(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, name: str
) -> None:
    await run_negative(
        session, redis_client, clock, SPECS[name], actor=None, case=Case(401, "SESSION_EXPIRED")
    )


@pytest.mark.auth
@pytest.mark.parametrize("name", list(SPECS))
async def test_visible_but_unauthorized_is_403(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, name: str
) -> None:
    spec = SPECS[name]
    await run_negative(
        session,
        redis_client,
        clock,
        spec,
        actor=spec.forbidden_actor,
        case=Case(403, "AUTHORIZATION_REQUIRED"),
    )


@pytest.mark.auth
@pytest.mark.parametrize("name", list(SPECS))
async def test_invisible_or_missing_is_404(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, name: str
) -> None:
    spec = SPECS[name]
    actor, case = spec.not_found
    await run_negative(session, redis_client, clock, spec, actor=actor, case=case)


@pytest.mark.parametrize(
    ("name", "label", "case"), _params(lambda s: [("guard", s.guard)] if s.guard else [])
)
async def test_guard(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, name: str, label: str, case: Case
) -> None:
    await run_negative(session, redis_client, clock, SPECS[name], actor=SPECS[name].ok_actor, case=case)


@pytest.mark.parametrize(("name", "label", "case"), _params(lambda s: list(s.conflicts.items())))
async def test_conflict_is_409(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, name: str, label: str, case: Case
) -> None:
    assert case.status == 409
    await run_negative(session, redis_client, clock, SPECS[name], actor=SPECS[name].ok_actor, case=case)


@pytest.mark.parametrize("name", list(SPECS))
async def test_malformed_request_is_422(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, name: str
) -> None:
    spec = SPECS[name]
    await run_negative(session, redis_client, clock, spec, actor=spec.ok_actor, case=spec.malformed)


@pytest.mark.parametrize("name", list(SPECS))
async def test_replay_returns_the_original_and_writes_once(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, name: str
) -> None:
    """D-215. The positive control for every negative above: the same call succeeds, and a retry with
    the same key returns the stored response without a second audit or outbox row."""
    spec = SPECS[name]
    w = await build_world(session)
    ids = await spec.setup(session, w, clock)
    key = str(uuid.uuid4())
    before = await written(session, w)

    first = await send(session, redis_client, clock, w, spec, ids, actor=spec.ok_actor, key=key)
    after_first = await written(session, w)
    replay = await send(session, redis_client, clock, w, spec, ids, actor=spec.ok_actor, key=key)

    assert first.status_code == spec.ok_status, first.text
    assert (replay.status_code, replay.json()) == (first.status_code, first.json())
    assert after_first[0] > before[0], "the command should have written an audit row"
    assert await written(session, w) == after_first, "a replay must not write again"


def test_every_command_in_the_matrix_has_every_mandatory_category() -> None:
    """401/403/404/422/replay are mandatory for every command; guard and 409 where the command has one."""
    for name, spec in SPECS.items():
        assert spec.ok_actor and spec.forbidden_actor and spec.not_found and spec.malformed, name
    assert {n for n, s in SPECS.items() if s.guard is None} == {"remove dependency"}
    assert {n for n, s in SPECS.items() if not s.conflicts} == {"create task", "remove dependency"}
