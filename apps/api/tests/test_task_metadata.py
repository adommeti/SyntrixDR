"""`PATCH /tasks/{id}` -- "Non-state editable Task metadata" (API_CONTRACT.md:158).

Editable: descriptive fields (title, description, expected_duration_minutes, sort_order) and the
D-226/D-209 requirement fields (evidence_required, evidence_min_count, verification_note_required,
needs_specific_validation). Never status (lifecycle commands only), Owning Team (fixed, DATA_MODEL.md
invariant 8), Current Assignee (`assign`, D-214), context, phase or parent -- the schema forbids them.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FakeClock
from app.core.errors import AppError
from app.tasks_dependencies.commands import EDITABLE_FIELDS, update_task_metadata
from app.tasks_dependencies.schemas import UpdateTaskRequest
from tests.factories import audit_actions, build_world, headers, http, login, outbox_types, seed_task

pytestmark = [pytest.mark.api]


async def _row(session: AsyncSession, task_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text(
                "SELECT title, description, expected_duration_minutes, sort_order, evidence_required, "
                "evidence_min_count, verification_note_required, needs_specific_validation, status, "
                "owning_team_id, current_assignee_user_id, version FROM tasks WHERE id = :t"
            ),
            {"t": task_id},
        )
    ).one()


async def _audit_after(session: AsyncSession, task_id: uuid.UUID, action: str) -> list[Any]:
    rows = await session.execute(
        text("SELECT before_data, after_data FROM audit_events WHERE entity_id = :t AND action = :a"),
        {"t": task_id, "a": action},
    )
    return list(rows.all())


async def test_coordinator_edits_descriptive_and_requirement_fields(
    session: AsyncSession, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    outbox_before = await outbox_types(session, task_id)

    await update_task_metadata(
        session,
        actor_id=w.coordinator_id,
        task_id=task_id,
        expected_version=1,
        changes={"title": "Fail over DNS", "evidence_min_count": 2, "needs_specific_validation": True},
        clock=clock,
    )

    row = await _row(session, task_id)
    assert (row.title, row.evidence_min_count, row.needs_specific_validation) == ("Fail over DNS", 2, True)
    assert (row.status, row.version) == ("NOT_STARTED", 2)
    [(before, after)] = await _audit_after(session, task_id, "TASK_UPDATED")
    assert before["evidence_min_count"] == 1 and after["evidence_min_count"] == 2
    assert set(after) == {"title", "evidence_min_count", "needs_specific_validation", "version"}
    assert await outbox_types(session, task_id) == [*outbox_before, "TaskChanged"]


async def test_explicit_null_clears_a_nullable_field(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    await update_task_metadata(
        session, actor_id=w.admin_id, task_id=task_id, expected_version=1,
        changes={"description": "runbook 7"}, clock=clock,
    )  # fmt: skip

    await update_task_metadata(
        session, actor_id=w.admin_id, task_id=task_id, expected_version=2,
        changes={"description": None}, clock=clock,
    )  # fmt: skip

    assert (await _row(session, task_id)).description is None


async def test_an_edit_that_changes_nothing_is_a_no_op(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    current = await _row(session, task_id)
    audit_before = await audit_actions(session, task_id)

    await update_task_metadata(
        session, actor_id=w.admin_id, task_id=task_id, expected_version=1,
        changes={"title": current.title, "evidence_required": current.evidence_required}, clock=clock,
    )  # fmt: skip

    assert (await _row(session, task_id)).version == 1
    assert await audit_actions(session, task_id) == audit_before


async def test_the_executor_edits_descriptive_fields_of_their_own_work(
    session: AsyncSession, clock: FakeClock
) -> None:
    """RBAC_MATRIX.md "Change Task metadata": Executor -- own/Team work."""
    w = await build_world(session)
    task_id = await seed_task(session, w, assignee_id=w.executor_id)

    await update_task_metadata(
        session, actor_id=w.executor_id, task_id=task_id, expected_version=1,
        changes={"title": "Renamed by executor", "expected_duration_minutes": 45}, clock=clock,
    )  # fmt: skip

    row = await _row(session, task_id)
    assert (row.title, row.expected_duration_minutes, row.version) == ("Renamed by executor", 45, 2)


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_min_count": 0},
        {"evidence_required": False},
        {"verification_note_required": False},
        {"needs_specific_validation": False},
        {"title": "x", "evidence_min_count": 0},
    ],
)
async def test_the_executor_cannot_change_what_their_own_completion_requires(
    session: AsyncSession, clock: FakeClock, changes: dict[str, Any]
) -> None:
    """Otherwise the assignee could lower evidence_min_count and step around D-226's guard."""
    w = await build_world(session)
    task_id = await seed_task(session, w, assignee_id=w.executor_id)

    with pytest.raises(AppError) as exc:
        await update_task_metadata(
            session, actor_id=w.executor_id, task_id=task_id, expected_version=1, changes=changes, clock=clock
        )

    assert exc.value.status_code == 403
    assert (await _row(session, task_id)).version == 1


async def test_an_executor_outside_the_owning_team_is_403(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await update_task_metadata(
            session, actor_id=w.outsider_id, task_id=task_id, expected_version=1,
            changes={"title": "x"}, clock=clock,
        )  # fmt: skip

    assert exc.value.status_code == 403


async def test_a_non_participant_gets_404(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await update_task_metadata(
            session, actor_id=w.stranger_id, task_id=task_id, expected_version=1,
            changes={"title": "x"}, clock=clock,
        )  # fmt: skip

    assert (exc.value.status_code, exc.value.code) == (404, "TASK_NOT_FOUND")


async def test_stale_version_is_409_and_nothing_changes(session: AsyncSession, clock: FakeClock) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)

    with pytest.raises(AppError) as exc:
        await update_task_metadata(
            session, actor_id=w.admin_id, task_id=task_id, expected_version=7,
            changes={"title": "x"}, clock=clock,
        )  # fmt: skip

    assert exc.value.code == "CONCURRENCY_CONFLICT"
    assert (await _row(session, task_id)).version == 1


async def test_a_submitted_task_keeps_its_requirements_but_takes_descriptive_edits(
    session: AsyncSession, clock: FakeClock
) -> None:
    """READY_FOR_VALIDATION: the submission was judged against the current requirement fields."""
    w = await build_world(session)
    task_id = await seed_task(session, w, status="READY_FOR_VALIDATION")

    with pytest.raises(AppError) as exc:
        await update_task_metadata(
            session, actor_id=w.admin_id, task_id=task_id, expected_version=1,
            changes={"evidence_min_count": 3}, clock=clock,
        )  # fmt: skip
    assert (exc.value.status_code, exc.value.code) == (409, "TASK_METADATA_LOCKED")
    assert exc.value.details == {"status": "READY_FOR_VALIDATION", "fields": ["evidence_min_count"]}

    await update_task_metadata(
        session, actor_id=w.admin_id, task_id=task_id, expected_version=1,
        changes={"description": "clarified"}, clock=clock,
    )  # fmt: skip
    assert (await _row(session, task_id)).description == "clarified"


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
async def test_a_terminal_task_is_not_editable(session: AsyncSession, clock: FakeClock, status: str) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w, status=status)

    with pytest.raises(AppError) as exc:
        await update_task_metadata(
            session, actor_id=w.admin_id, task_id=task_id, expected_version=1,
            changes={"title": "x"}, clock=clock,
        )  # fmt: skip

    assert (exc.value.status_code, exc.value.code) == (409, "TASK_METADATA_LOCKED")
    assert (await _row(session, task_id)).version == 1


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        {"status": "COMPLETED"},
        {"owning_team_id": "00000000-0000-0000-0000-000000000001"},
        {"current_assignee_user_id": "00000000-0000-0000-0000-000000000001"},
        {"work_stream_id": "00000000-0000-0000-0000-000000000001"},
        {"dr_application_id": None},
        {"phase": "FAILBACK"},
        {"parent_task_id": None},
        {"version": 5},
        {"title": "   "},
        {"title": None},
        {"evidence_min_count": -1},
        {"evidence_min_count": 40_000},
        {"expected_duration_minutes": 2**31},
        {"sort_order": 2**31},
    ],
)
async def test_patch_rejects_non_metadata_and_invalid_fields(
    session: AsyncSession, redis_client: Redis, clock: FakeClock, field: dict[str, Any]
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    before = await _row(session, task_id)
    sid, csrf = await login(session, redis_client, clock, w.admin_id)

    async with http(session, redis_client, clock) as c:
        r = await c.patch(
            f"/api/v1/tasks/{task_id}",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={"expected_version": 1, **field},
        )

    assert r.status_code == 422, r.text
    assert await _row(session, task_id) == before
    await redis_client.delete(f"drcc:session:{sid}")


async def test_patch_over_http_updates_and_replays(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    task_id = await seed_task(session, w)
    sid, csrf = await login(session, redis_client, clock, w.coordinator_id)
    key_headers = headers(csrf)
    payload = {"expected_version": 1, "title": "Cut over storage", "description": None}

    async with http(session, redis_client, clock) as c:
        first = await c.patch(
            f"/api/v1/tasks/{task_id}", cookies={"drcc_session": sid}, headers=key_headers, json=payload
        )
        again = await c.patch(
            f"/api/v1/tasks/{task_id}", cookies={"drcc_session": sid}, headers=key_headers, json=payload
        )

    assert first.status_code == 200, first.text
    assert first.json()["title"] == "Cut over storage"
    assert first.json()["version"] == 2
    assert again.json() == first.json()
    assert len(await _audit_after(session, task_id, "TASK_UPDATED")) == 1
    await redis_client.delete(f"drcc:session:{sid}")


def test_the_patch_schema_and_the_command_allow_the_same_fields() -> None:
    """Two lists of one rule: the schema's `extra="forbid"` and the command's allowlist must not drift."""
    assert set(UpdateTaskRequest.model_fields) - {"expected_version"} == EDITABLE_FIELDS


async def test_create_rejects_an_out_of_range_evidence_count(
    session: AsyncSession, redis_client: Redis, clock: FakeClock
) -> None:
    w = await build_world(session)
    sid, csrf = await login(session, redis_client, clock, w.coordinator_id)

    async with http(session, redis_client, clock) as c:
        r = await c.post(
            f"/api/v1/dr-events/{w.event_id}/tasks",
            cookies={"drcc_session": sid},
            headers=headers(csrf),
            json={
                "title": "New",
                "phase": "FAILOVER",
                "owning_team_id": str(w.team_id),
                "work_stream_id": str(w.work_stream_id),
                "evidence_min_count": 40_000,
            },
        )

    assert r.status_code == 422, r.text
    await redis_client.delete(f"drcc:session:{sid}")
