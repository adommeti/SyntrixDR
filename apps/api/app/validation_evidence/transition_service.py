from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.clock import Clock
from app.core.errors import AppError
from app.validation_evidence.models import Validation


class InvalidValidationTransitionError(AppError):
    code = "INVALID_TRANSITION"
    status_code = 409

    def __init__(self, current: str) -> None:
        super().__init__(f"Cannot close a Validation that is {current}; only PENDING can be closed.")


class ValidationTransitionService:
    """`PENDING -> APPROVED | REJECTED` (STATE_MACHINES.md §Validation, D-210). IN_REVIEW and
    REMEDIATION stay in the enum but are unused in V1, so nothing here reaches them."""

    @staticmethod
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
        """Called only from `TaskTransitionService.validate`, which has already enforced D-209's
        validator rule and holds the row lock -- an already-authorized side effect."""
        if validation.status != "PENDING":
            raise InvalidValidationTransitionError(validation.status)

        now = clock.now()
        before = {"status": validation.status}
        validation.status = "APPROVED" if approve else "REJECTED"
        validation.validator_user_id = validator_user_id
        validation.reviewed_at = now
        if approve:
            validation.approved_at = now
        validation.note = note
        validation.version += 1
        validation.updated_at = now
        await session.flush()

        await write_audit(
            session,
            actor_user_id=validator_user_id,
            entity_type="VALIDATION",
            entity_id=validation.id,
            action="VALIDATION_APPROVED" if approve else "VALIDATION_REJECTED",
            dr_event_id=dr_event_id,
            before=before,
            after={"status": validation.status, "version": validation.version},
        )
        return validation

    @staticmethod
    async def close_for_cancelled_task(
        session: AsyncSession,
        *,
        validation: Validation,
        actor_id: uuid.UUID,
        reason: str,
        dr_event_id: uuid.UUID,
        clock: Clock,
    ) -> Validation:
        """Called only from `TaskTransitionService.cancel` (already authorized, row locked). D-210's
        only terminal states are APPROVED | REJECTED, and `validate` refuses a CANCELLED Task, so the
        submission closes REJECTED here. `validator_user_id` stays NULL: nobody reviewed the work."""
        if validation.status != "PENDING":
            raise InvalidValidationTransitionError(validation.status)

        now = clock.now()
        before = {"status": validation.status}
        validation.status = "REJECTED"
        validation.reviewed_at = now
        validation.note = f"Task cancelled: {reason}"
        validation.version += 1
        validation.updated_at = now
        await session.flush()

        await write_audit(
            session,
            actor_user_id=actor_id,
            entity_type="VALIDATION",
            entity_id=validation.id,
            action="VALIDATION_CLOSED_TASK_CANCELLED",
            dr_event_id=dr_event_id,
            before=before,
            after={"status": validation.status, "version": validation.version, "reason": reason},
        )
        return validation
