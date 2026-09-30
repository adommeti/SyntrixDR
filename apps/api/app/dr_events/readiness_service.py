from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.queries import list_application_ids_with_primary_owner
from app.dr_events.models import DrApplication, DrEvent
from app.policies_admin.queries import PolicyService
from app.tasks_dependencies.queries import event_graph_has_cycle, live_monitoring_task_exists, owning_team_ids
from app.users_teams_org.queries import live_team_ids
from app.work_streams.queries import every_stream_has_a_lead

#: D-224 readiness catalog has 13 keys (FREEZE_ADDENDUM.md:60); these 10 are computable today. BUILD-06
#: added `.dependency_graph_acyclic`, `.task_owning_team`, `.work_stream_lead` and
#: `.monitoring_task_present`. The other 3 (`readiness.failback_plan_exists`,
#: `.critical_milestone_owner`, `.needs_review_resolved`) still reference entities or commands that
#: don't exist yet, and are deliberately never evaluated here, not faked as passing.
EVALUATED_KEYS = (
    "readiness.event_timezone_set",
    "readiness.coordinator_assigned",
    "readiness.application_in_scope",
    "readiness.primary_system_owner",
    "readiness.rpo_target_or_na",
    "readiness.primary_business_owner",
    "readiness.dependency_graph_acyclic",
    "readiness.task_owning_team",
    "readiness.work_stream_lead",
    "readiness.monitoring_task_present",
)

#: D-224: "dependency graph acyclic — HARD_STOP and not configurable". A cycle is invalid outright
#: (FROZEN_DECISIONS.md §7.9), so this key's severity is fixed here whatever `policy_values` says and
#: no `override_reason` gets past it (BUILD-06.plan.md Risk #14).
NON_OVERRIDABLE_KEYS = frozenset({"readiness.dependency_graph_acyclic"})


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
    team_ids = await owning_team_ids(session, event.id)

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
        "readiness.dependency_graph_acyclic": not await event_graph_has_cycle(session, event.id),
        # tasks.owning_team_id is NOT NULL, so the only way this fails is the Team being soft-deleted.
        "readiness.task_owning_team": set(team_ids) <= await live_team_ids(session, team_ids),
        "readiness.work_stream_lead": await every_stream_has_a_lead(session, event.id),
        "readiness.monitoring_task_present": await live_monitoring_task_exists(session, event.id),
    }

    results: list[ReadinessResult] = []
    for key in EVALUATED_KEYS:
        severity = (
            "HARD_STOP"
            if key in NON_OVERRIDABLE_KEYS
            else await PolicyService.resolve(session, key, event_id=event.id)
        )
        results.append(ReadinessResult(key=key, severity=severity, satisfied=satisfied_by_key[key]))
    return results
