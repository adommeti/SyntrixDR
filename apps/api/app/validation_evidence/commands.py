from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock
from app.validation_evidence.models import Validation
from app.validation_evidence.queries import get_pending_task_validation
from app.validation_evidence.transition_service import ValidationTransitionService


async def open_task_validation(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    verification_note: str | None,
    submitted_by_user_id: uuid.UUID,
    clock: Clock,
) -> Validation:
    """Creates the persisted PENDING Validation record (D-210). Called only from
    `TaskTransitionService.submit_validation` after its own authorization and D-226 guards, so no
    separate check here. Status comes from the column default (PENDING)."""
    now = clock.now()
    validation = Validation(
        target_type="TASK",
        target_id=task_id,
        verification_note=verification_note,
        submitted_at=now,
        created_by_user_id=submitted_by_user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(validation)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=submitted_by_user_id,
        entity_type="VALIDATION",
        entity_id=validation.id,
        action="VALIDATION_OPENED",
        dr_event_id=dr_event_id,
        after={"target_type": "TASK", "target_id": str(task_id), "status": validation.status},
    )
    return validation


async def close_task_validation(
    session: AsyncSession,
    *,
    validation: Validation,
    approve: bool,
    validator_user_id: uuid.UUID,
    note: str | None,
    dr_event_id: uuid.UUID,
    clock: Clock,
) -> Validation:
    """Application-layer entry point other modules call, so they never import this module's
    transition service directly (cross-module rule: commands.py/queries.py only)."""
    return await ValidationTransitionService.close_task_validation(
        session,
        validation=validation,
        approve=approve,
        validator_user_id=validator_user_id,
        note=note,
        dr_event_id=dr_event_id,
        clock=clock,
    )


async def close_validation_for_cancelled_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    reason: str,
    dr_event_id: uuid.UUID,
    clock: Clock,
) -> Validation | None:
    """Closes the Task's open submission, if any, as part of `cancel` (same transaction)."""
    validation = await get_pending_task_validation(session, task_id, for_update=True)
    if validation is None:
        return None
    return await ValidationTransitionService.close_for_cancelled_task(
        session,
        validation=validation,
        actor_id=actor_id,
        reason=reason,
        dr_event_id=dr_event_id,
        clock=clock,
    )
