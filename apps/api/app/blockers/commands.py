from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers.models import Blocker
from app.core.audit import write_audit
from app.core.clock import Clock


async def create_blocker_for_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    dr_event_id: uuid.UUID,
    reason: str,
    created_by_user_id: uuid.UUID,
    clock: Clock,
) -> Blocker:
    """Called only from `TaskTransitionService.block`, inside its transaction and after its own
    authorization -- an already-authorized side effect, so no separate check here (same precedent
    as `plans_import/commands.py::capture_baseline_snapshot`). Status comes from the column
    default (OPEN); this module never writes a Blocker status outside a transition service."""
    now = clock.now()
    blocker = Blocker(
        task_id=task_id,
        reason=reason,
        created_by_user_id=created_by_user_id,
        blocked_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(blocker)
    await session.flush()
    await write_audit(
        session,
        actor_user_id=created_by_user_id,
        entity_type="BLOCKER",
        entity_id=blocker.id,
        action="BLOCKER_CREATED",
        dr_event_id=dr_event_id,
        after={"task_id": str(task_id), "status": blocker.status, "reason": reason},
    )
    return blocker
