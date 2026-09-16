from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import list_application_ids_with_primary_owner
from app.dr_events.models import DrApplication, DrEvent
from app.policies_admin.queries import PolicyService

#: D-224 readiness catalog has 13 keys (FREEZE_ADDENDUM.md:60); only these 6 are computable with
#: entities that exist today. The other 7 (`readiness.failback_plan_exists`,
#: `.critical_milestone_owner`, `.work_stream_lead`, `.task_owning_team`,
#: `.dependency_graph_acyclic`, `.monitoring_task_present`, `.needs_review_resolved`) reference
#: Work Streams/Tasks/Milestones/imports — modules that don't exist yet — and are deliberately
#: never evaluated here, not faked as passing (BUILD-04.plan.md session-b scope).
EVALUATED_KEYS = (
    "readiness.event_timezone_set",
    "readiness.coordinator_assigned",
    "readiness.application_in_scope",
    "readiness.primary_system_owner",
    "readiness.rpo_target_or_na",
    "readiness.primary_business_owner",
)


@dataclass(frozen=True)
class ReadinessResult:
    key: str
    severity: str  # HARD_STOP | WARNING | OFF, resolved live from policy_values (D-224)
    satisfied: bool


async def _every_in_scope_app_has_primary_owner(
    session: AsyncSession, dr_apps: list[DrApplication], owner_type: str
) -> bool:
    if not dr_apps:
        # `readiness.application_in_scope` is not a locked policy key (policies_admin/commands.py's
        # `_LOCKED_KEYS`), so a Coordinator can downgrade it to WARNING/OFF at Event scope. Returning
        # True here would then let owner/RPO checks silently pass on an Event with zero Applications.
        return False
    application_ids = [a.application_id for a in dr_apps]
    covered = await list_application_ids_with_primary_owner(session, application_ids, owner_type)
    return all(application_id in covered for application_id in application_ids)


async def evaluate_readiness(session: AsyncSession, event: DrEvent) -> list[ReadinessResult]:
    """Evaluates `EVALUATED_KEYS` against `event`'s current state, each key's severity resolved
    live from `PolicyService.resolve` (Admin global default / Coordinator Event-scoped
    override) so a policy change takes effect on the next `activate` call with no code change."""
    dr_apps_result = await session.execute(
        select(DrApplication).where(DrApplication.dr_event_id == event.id, DrApplication.deleted_at.is_(None))
    )
    dr_apps = list(dr_apps_result.scalars().all())

    satisfied_by_key = {
        "readiness.event_timezone_set": bool(event.event_timezone),
        "readiness.coordinator_assigned": event.coordinator_user_id is not None,
        "readiness.application_in_scope": len(dr_apps) > 0,
        "readiness.primary_system_owner": await _every_in_scope_app_has_primary_owner(
            session, dr_apps, "SYSTEM_APPLICATION"
        ),
        "readiness.primary_business_owner": await _every_in_scope_app_has_primary_owner(
            session, dr_apps, "BUSINESS"
        ),
        "readiness.rpo_target_or_na": bool(dr_apps)
        and all(a.rpo_target_minutes is not None or a.rpo_not_applicable for a in dr_apps),
    }

    results: list[ReadinessResult] = []
    for key in EVALUATED_KEYS:
        severity = await PolicyService.resolve(session, key, event_id=event.id)
        results.append(ReadinessResult(key=key, severity=severity, satisfied=satisfied_by_key[key]))
    return results
