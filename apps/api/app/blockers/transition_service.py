"""The only place a Blocker's `status` changes (STATE_MACHINES.md §Blocker, D-211, D-252).

`OPEN -> ASSIGNED -> IN_PROGRESS -> RESOLVED -> VERIFIED -> CLOSED`

Four commands drive six states: `assign`, `start`, `resolve`, `verify`. `verify` writes VERIFIED and
then CLOSED in one transaction, each with its own audit and outbox row (invariant 11), and -- when it
closes the Task's last active Blocker -- returns the Task `BLOCKED -> IN_PROGRESS` in that same
transaction through `tasks_dependencies.commands.resume_task_after_last_blocker` (I-1). A verify that
leaves another Blocker active does not touch the Task. The Task's Owning Team is never written here:
routing a Blocker to another Team is not a change of accountability (D-211).

Order per command (`/drcc-transition-service`): lock the Task (visibility 404) and the Blocker (FOR
UPDATE) -> authorize -> version -> legality -> business guards -> mutate -> audit -> outbox.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers.models import Blocker
from app.blockers.policies import (
    actor_may_resolve_blocker,
    actor_may_route_blocker,
    actor_may_start_blocker,
    actor_may_verify_blocker,
)
from app.blockers.queries import count_active_blockers, lock_blocker
from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError
from app.core.outbox import write_outbox
from app.dr_events.participants import enrol_participant
from app.tasks_dependencies.commands import resume_task_after_last_blocker
from app.tasks_dependencies.models import Task
from app.tasks_dependencies.queries import lock_visible_task
from app.users_teams_org.authorization import AuthorizationRequiredError
from app.users_teams_org.commands import UserNotFoundError
from app.users_teams_org.queries import get_team, is_active_team_member, is_user_active

OPEN = "OPEN"
ASSIGNED = "ASSIGNED"
IN_PROGRESS = "IN_PROGRESS"
RESOLVED = "RESOLVED"
VERIFIED = "VERIFIED"
CLOSED = "CLOSED"

#: Legal from-states per command. `assign` re-routes an ASSIGNED Blocker too (before work starts).
_LEGAL_FROM: dict[str, frozenset[str]] = {
    "assign": frozenset({OPEN, ASSIGNED}),
    "start": frozenset({ASSIGNED}),
    "resolve": frozenset({IN_PROGRESS}),
    "verify": frozenset({RESOLVED}),
}


class BlockerNotFoundError(AppError):
    code = "BLOCKER_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Blocker not found.")


class InvalidBlockerTransitionError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409

    def __init__(self, command: str, current: str) -> None:
        super().__init__(
            f"Cannot {command} a Blocker that is {current}.", details={"command": command, "status": current}
        )


class BlockerRoutingRequiredError(AppError):
    code = "BLOCKER_ROUTING_REQUIRED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("Assigning a Blocker needs a Team queue, a resolver, or both.")


class BlockerRoutingMismatchError(AppError):
    code = "BLOCKER_ROUTING_MISMATCH"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("The resolver must be an active member of the Team the Blocker is routed to.")


class BlockerTeamNotFoundError(AppError):
    code = "TEAM_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Team not found.")


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def _snapshot(blocker: Blocker) -> dict[str, Any]:
    return {
        "status": blocker.status,
        "version": blocker.version,
        "blocker_team_id": str(blocker.blocker_team_id) if blocker.blocker_team_id else None,
        "blocker_owner_user_id": str(blocker.blocker_owner_user_id)
        if blocker.blocker_owner_user_id
        else None,
    }


async def _load(session: AsyncSession, actor_id: uuid.UUID, blocker_id: uuid.UUID) -> tuple[Task, Blocker]:
    """Blocker and its Task, both `FOR UPDATE` (Task first: the lock order every Task command uses, so a
    concurrent `verify` on a sibling Blocker serialises on the Task row). 404 when the Blocker is
    missing *or* its Event is invisible to the actor (same shape as the Task commands)."""
    blocker = await lock_blocker(session, blocker_id)
    if blocker is None:
        raise BlockerNotFoundError()
    task = await lock_visible_task(session, actor_id, blocker.task_id)
    if task is None:
        raise BlockerNotFoundError()
    # Re-lock the Blocker after the Task so the lock order is Task -> Blocker for every caller.
    locked = await lock_blocker(session, blocker_id)
    assert locked is not None
    return task, locked


def _check_version(blocker: Blocker, expected_version: int) -> None:
    if blocker.version != expected_version:
        raise ConcurrencyConflictError()


def _assert_legal(command: str, blocker: Blocker) -> None:
    if blocker.status not in _LEGAL_FROM[command]:
        raise InvalidBlockerTransitionError(command, blocker.status)


async def _finish(
    session: AsyncSession,
    *,
    blocker: Blocker,
    task: Task,
    actor_id: uuid.UUID | None,
    before: dict[str, Any],
    action: str,
    clock: Clock,
    extra: dict[str, Any] | None = None,
    actor_type: str = "USER",
) -> Blocker:
    """Shared tail -- version, audit, outbox -- for exactly one status change. The status write stays in
    each caller so every transition keeps one visible `blocker.status = ...` line."""
    blocker.version += 1
    blocker.updated_at = clock.now()
    await session.flush()
    after = {**_snapshot(blocker), **(extra or {})}
    await write_audit(
        session,
        actor_user_id=actor_id,
        actor_type=actor_type,
        entity_type="BLOCKER",
        entity_id=blocker.id,
        action=action,
        dr_event_id=task.dr_event_id,
        before=before,
        after=after,
    )
    await write_outbox(
        session,
        aggregate_type="BLOCKER",
        aggregate_id=blocker.id,
        event_type="BlockerChanged",  # D-234 V1 union (API_CONTRACT.md:274)
        payload={"change": action, "task_id": str(task.id), **after},
        clock=clock,
        dr_event_id=task.dr_event_id,
    )
    return blocker


async def _enrol_owner(session: AsyncSession, task: Task, owner_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    await enrol_participant(session, task.dr_event_id, owner_id, "BLOCKER_OWNER", added_by_user_id=actor_id)


class BlockerTransitionService:
    @staticmethod
    async def assign(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        blocker_id: uuid.UUID,
        expected_version: int,
        team_id: uuid.UUID | None = None,
        assignee_user_id: uuid.UUID | None = None,
        clock: Clock | None = None,
    ) -> Blocker:
        """OPEN/ASSIGNED -> ASSIGNED: route to a Team queue (FROZEN §108, one queue per Team) and/or a
        resolver. Routing never changes the Task's Owning Team (D-211). The resolver is enrolled as a
        BLOCKER_OWNER participant (D-222)."""
        clock = clock or SystemClock()
        now: datetime = clock.now()
        task, blocker = await _load(session, actor_id, blocker_id)
        if not await actor_may_route_blocker(
            session, actor_id, task, blocker, team_id=team_id, assignee_user_id=assignee_user_id, at=now
        ):
            raise AuthorizationRequiredError()
        _check_version(blocker, expected_version)
        _assert_legal("assign", blocker)
        if team_id is None and assignee_user_id is None:
            raise BlockerRoutingRequiredError()
        if team_id is not None and await get_team(session, team_id) is None:
            raise BlockerTeamNotFoundError()
        if assignee_user_id is not None:
            if not await is_user_active(session, assignee_user_id):
                raise UserNotFoundError()
            if team_id is not None and not await is_active_team_member(
                session, team_id, assignee_user_id, at=now
            ):
                raise BlockerRoutingMismatchError()

        before = _snapshot(blocker)
        blocker.status = ASSIGNED
        if team_id is not None:
            blocker.blocker_team_id = team_id
        # A plain Team routing clears a previous resolver: the queue decides who picks it up.
        blocker.blocker_owner_user_id = assignee_user_id
        if assignee_user_id is not None:
            await _enrol_owner(session, task, assignee_user_id, actor_id)
        return await _finish(
            session,
            blocker=blocker,
            task=task,
            actor_id=actor_id,
            before=before,
            action="BLOCKER_ASSIGNED",
            clock=clock,
        )

    @staticmethod
    async def start(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        blocker_id: uuid.UUID,
        expected_version: int,
        clock: Clock | None = None,
    ) -> Blocker:
        """ASSIGNED -> IN_PROGRESS: the resolver begins work (`POST /blockers/{id}/start`, added by
        D-211). Starting an unowned queue item claims it for the actor."""
        clock = clock or SystemClock()
        now = clock.now()
        task, blocker = await _load(session, actor_id, blocker_id)
        if not await actor_may_start_blocker(session, actor_id, task, blocker, at=now):
            raise AuthorizationRequiredError()
        _check_version(blocker, expected_version)
        _assert_legal("start", blocker)

        before = _snapshot(blocker)
        blocker.status = IN_PROGRESS
        blocker.claimed_at = now
        if blocker.blocker_owner_user_id is None:
            blocker.blocker_owner_user_id = actor_id
            await _enrol_owner(session, task, actor_id, actor_id)
        return await _finish(
            session,
            blocker=blocker,
            task=task,
            actor_id=actor_id,
            before=before,
            action="BLOCKER_STARTED",
            clock=clock,
        )

    @staticmethod
    async def resolve(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        blocker_id: uuid.UUID,
        expected_version: int,
        resolution_note: str | None = None,
        clock: Clock | None = None,
    ) -> Blocker:
        """IN_PROGRESS -> RESOLVED: the resolver declares it fixed. The Owning Team still has to verify."""
        clock = clock or SystemClock()
        now = clock.now()
        task, blocker = await _load(session, actor_id, blocker_id)
        if not await actor_may_resolve_blocker(session, actor_id, task, blocker, at=now):
            raise AuthorizationRequiredError()
        _check_version(blocker, expected_version)
        _assert_legal("resolve", blocker)

        before = _snapshot(blocker)
        blocker.status = RESOLVED
        blocker.resolved_at = now
        blocker.resolution_note = _clean(resolution_note)
        return await _finish(
            session,
            blocker=blocker,
            task=task,
            actor_id=actor_id,
            before=before,
            action="BLOCKER_RESOLVED",
            clock=clock,
        )

    @staticmethod
    async def verify(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        blocker_id: uuid.UUID,
        expected_version: int,
        clock: Clock | None = None,
    ) -> Blocker:
        """RESOLVED -> VERIFIED -> CLOSED, atomically, both audited (D-211). Closing the Task's last
        active Blocker returns the Task BLOCKED -> IN_PROGRESS in this same transaction (D-252, I-1)."""
        clock = clock or SystemClock()
        now = clock.now()
        task, blocker = await _load(session, actor_id, blocker_id)
        if not await actor_may_verify_blocker(session, actor_id, task, blocker, at=now):
            raise AuthorizationRequiredError()
        _check_version(blocker, expected_version)
        _assert_legal("verify", blocker)

        before = _snapshot(blocker)
        blocker.status = VERIFIED
        blocker.verified_at = now
        await _finish(
            session,
            blocker=blocker,
            task=task,
            actor_id=actor_id,
            before=before,
            action="BLOCKER_VERIFIED",
            clock=clock,
        )

        before = _snapshot(blocker)
        blocker.status = CLOSED
        blocker.closed_at = now
        await _finish(
            session,
            blocker=blocker,
            task=task,
            actor_id=actor_id,
            before=before,
            action="BLOCKER_CLOSED",
            clock=clock,
        )

        if task.status == "BLOCKED" and await count_active_blockers(session, task.id) == 0:
            await resume_task_after_last_blocker(
                session, task_id=task.id, actor_id=actor_id, blocker_id=blocker.id, clock=clock
            )
        return blocker
