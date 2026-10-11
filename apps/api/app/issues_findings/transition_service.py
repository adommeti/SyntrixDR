"""The only place an Issue/Finding's `status` changes (STATE_MACHINES.md §Issue/Finding, D-253):

`OPEN -> IN_REVIEW -> RESOLVED | DISMISSED`

`review` is OPEN only; `resolve` and `dismiss` close from OPEN or IN_REVIEW and need a note
(API_CONTRACT.md:187-188 "with note"). RESOLVED and DISMISSED are terminal. No transition here touches
a Task: an Issue never stops execution (D-253)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError, ConcurrencyConflictError
from app.core.outbox import write_outbox
from app.dr_events.participants import user_can_see_event
from app.issues_findings.models import IssueFinding
from app.issues_findings.policies import actor_may_handle_issue
from app.issues_findings.queries import lock_issue
from app.users_teams_org.authorization import AuthorizationRequiredError

OPEN = "OPEN"
IN_REVIEW = "IN_REVIEW"
RESOLVED = "RESOLVED"
DISMISSED = "DISMISSED"

_LEGAL_FROM: dict[str, frozenset[str]] = {
    "review": frozenset({OPEN}),
    "resolve": frozenset({OPEN, IN_REVIEW}),
    "dismiss": frozenset({OPEN, IN_REVIEW}),
}


class IssueNotFoundError(AppError):
    code = "ISSUE_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Issue/Finding not found.")


class InvalidIssueTransitionError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409

    def __init__(self, command: str, current: str) -> None:
        super().__init__(
            f"Cannot {command} an Issue/Finding that is {current}.",
            details={"command": command, "status": current},
        )


class ResolutionNoteRequiredError(AppError):
    code = "RESOLUTION_NOTE_REQUIRED"
    status_code = 422

    def __init__(self, command: str) -> None:
        super().__init__(f"A note is required to {command} an Issue/Finding.")


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def _snapshot(issue: IssueFinding) -> dict[str, Any]:
    return {"status": issue.status, "version": issue.version}


async def _load(session: AsyncSession, actor_id: uuid.UUID, issue_id: uuid.UUID) -> IssueFinding:
    issue = await lock_issue(session, issue_id)
    if issue is None or not await user_can_see_event(session, actor_id, issue.dr_event_id):
        raise IssueNotFoundError()
    return issue


async def _finish(
    session: AsyncSession,
    *,
    issue: IssueFinding,
    actor_id: uuid.UUID,
    before: dict[str, Any],
    action: str,
    clock: Clock,
    extra: dict[str, Any] | None = None,
) -> IssueFinding:
    issue.version += 1
    issue.updated_at = clock.now()
    await session.flush()
    after = {**_snapshot(issue), **(extra or {})}
    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="ISSUE_FINDING",
        entity_id=issue.id,
        action=action,
        dr_event_id=issue.dr_event_id,
        before=before,
        after=after,
    )
    await write_outbox(
        session,
        aggregate_type="ISSUE_FINDING",
        aggregate_id=issue.id,
        event_type="IssueChanged",  # D-234 V1 union
        payload={"change": action, **after},
        clock=clock,
        dr_event_id=issue.dr_event_id,
    )
    return issue


async def _prepare(
    session: AsyncSession, *, actor_id: uuid.UUID, issue_id: uuid.UUID, expected_version: int, command: str
) -> IssueFinding:
    issue = await _load(session, actor_id, issue_id)
    if not await actor_may_handle_issue(session, actor_id, issue):
        raise AuthorizationRequiredError()
    if issue.version != expected_version:
        raise ConcurrencyConflictError()
    if issue.status not in _LEGAL_FROM[command]:
        raise InvalidIssueTransitionError(command, issue.status)
    return issue


class IssueTransitionService:
    @staticmethod
    async def review(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        issue_id: uuid.UUID,
        expected_version: int,
        clock: Clock | None = None,
    ) -> IssueFinding:
        """OPEN -> IN_REVIEW (API_CONTRACT.md:186)."""
        clock = clock or SystemClock()
        issue = await _prepare(
            session, actor_id=actor_id, issue_id=issue_id, expected_version=expected_version, command="review"
        )
        before = _snapshot(issue)
        issue.status = IN_REVIEW
        return await _finish(
            session, issue=issue, actor_id=actor_id, before=before, action="ISSUE_IN_REVIEW", clock=clock
        )

    @staticmethod
    async def resolve(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        issue_id: uuid.UUID,
        expected_version: int,
        note: str | None,
        clock: Clock | None = None,
    ) -> IssueFinding:
        """OPEN/IN_REVIEW -> RESOLVED with a note (API_CONTRACT.md:187)."""
        return await _close(
            session, actor_id=actor_id, issue_id=issue_id, expected_version=expected_version,
            note=note, command="resolve", target=RESOLVED, action="ISSUE_RESOLVED", clock=clock,
        )  # fmt: skip

    @staticmethod
    async def dismiss(
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        issue_id: uuid.UUID,
        expected_version: int,
        note: str | None,
        clock: Clock | None = None,
    ) -> IssueFinding:
        """OPEN/IN_REVIEW -> DISMISSED with a note (API_CONTRACT.md:188)."""
        return await _close(
            session, actor_id=actor_id, issue_id=issue_id, expected_version=expected_version,
            note=note, command="dismiss", target=DISMISSED, action="ISSUE_DISMISSED", clock=clock,
        )  # fmt: skip


async def _close(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    issue_id: uuid.UUID,
    expected_version: int,
    note: str | None,
    command: str,
    target: str,
    action: str,
    clock: Clock | None,
) -> IssueFinding:
    clock = clock or SystemClock()
    issue = await _prepare(
        session, actor_id=actor_id, issue_id=issue_id, expected_version=expected_version, command=command
    )
    clean_note = _clean(note)
    if clean_note is None:
        raise ResolutionNoteRequiredError(command)
    before = _snapshot(issue)
    issue.status = target
    issue.resolution_note = clean_note
    issue.resolved_at = clock.now()
    return await _finish(
        session,
        issue=issue,
        actor_id=actor_id,
        before=before,
        action=action,
        clock=clock,
        extra={"note": clean_note},
    )
