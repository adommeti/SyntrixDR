from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.applications_catalog.commands import ApplicationNotFoundError, TierNotFoundError
from app.applications_catalog.queries import get_application, get_tier, list_application_owners
from app.core.audit import write_audit
from app.core.clock import Clock, SystemClock
from app.core.errors import AppError
from app.core.outbox import write_outbox
from app.dr_events.models import DrApplication, DrEvent
from app.dr_events.participants import enrol_participant
from app.plans_import.commands import instantiate_plan_into_event
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope

_OWNER_TYPE_TO_PARTICIPANT_SOURCE = {
    "SYSTEM_APPLICATION": "APP_OWNER",
    "BUSINESS": "BUSINESS_OWNER",
}


class DrEventNotFoundError(AppError):
    code = "DR_EVENT_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("DR Event not found.")


class PlanVersionRequiredError(AppError):
    code = "PLAN_VERSION_REQUIRED"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("plan_version_id is required when plan_id is supplied.")


async def create_event(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    name: str,
    event_type: str,
    description: str | None = None,
    application_ids: list[uuid.UUID] | None = None,
    plan_id: uuid.UUID | None = None,
    plan_version_id: uuid.UUID | None = None,
    parent_dr_event_id: uuid.UUID | None = None,
    clock: Clock | None = None,
) -> DrEvent:
    """D-213: Global Admin/DR Coordinator may create at any scope (`CREATE_PLANNED_DR_EVENT`
    marker `True`); an App/System Owner may only create scoped to Application(s) they hold that
    capability's `"SCOPE"` marker for — checked per requested `application_id`, so a request
    naming even one Application the actor doesn't own is denied outright, not silently narrowed."""
    clock = clock or SystemClock()
    application_ids = application_ids or []

    if application_ids:
        for app_id in application_ids:
            await AuthorizationService.require(
                session,
                actor_id,
                Capability.CREATE_PLANNED_DR_EVENT,
                Scope(scope_type="APPLICATION", scope_id=app_id),
            )
    else:
        await AuthorizationService.require(session, actor_id, Capability.CREATE_PLANNED_DR_EVENT, Scope())

    if parent_dr_event_id is not None and await session.get(DrEvent, parent_dr_event_id) is None:
        raise DrEventNotFoundError()

    event = DrEvent(
        name=name,
        event_type=event_type,
        description=description,
        parent_dr_event_id=parent_dr_event_id,
        created_by_user_id=actor_id,
    )
    session.add(event)
    await session.flush()

    # D-222 participant visibility (dr_events/queries.py::list_visible_events/get_visible_event)
    # only grants implicit access to Global Admin/GLOBAL_READONLY -- everyone else (a Coordinator
    # or an App/System Owner, per D-213) needs an explicit `dr_event_participants` row or they
    # can't see the Event they were just authorized to create (found in review). The creator
    # always gets an EXPLICIT row; each in-scope Application's current owners get the
    # APP_OWNER/BUSINESS_OWNER source per D-222's own auto-enrolment list.
    await enrol_participant(session, event.id, actor_id, "EXPLICIT", added_by_user_id=actor_id)

    for app_id in application_ids:
        application = await get_application(session, app_id)
        if application is None:
            raise ApplicationNotFoundError()
        tier = await get_tier(session, application.tier_id)
        if tier is None:
            raise TierNotFoundError()

        dr_application = DrApplication(
            dr_event_id=event.id,
            application_id=app_id,
            effective_tier_id=tier.id,
            effective_sla_minutes=tier.default_sla_minutes,
            rto_target_minutes=tier.default_sla_minutes,
        )
        session.add(dr_application)
        await session.flush()
        await write_audit(
            session,
            actor_user_id=actor_id,
            entity_type="DR_APPLICATION",
            entity_id=dr_application.id,
            action="DR_APPLICATION_CREATED",
            after={"application_id": str(app_id), "dr_event_id": str(event.id)},
        )

        for owner in await list_application_owners(session, app_id):
            source = _OWNER_TYPE_TO_PARTICIPANT_SOURCE[owner.owner_type]
            await enrol_participant(session, event.id, owner.user_id, source, added_by_user_id=actor_id)

    if plan_id is not None:
        if plan_version_id is None:
            raise PlanVersionRequiredError()
        await instantiate_plan_into_event(
            session,
            actor_id=actor_id,
            plan_id=plan_id,
            plan_version_id=plan_version_id,
            dr_event_id=event.id,
            clock=clock,
        )

    await write_audit(
        session,
        actor_user_id=actor_id,
        entity_type="DR_EVENT",
        entity_id=event.id,
        action="DR_EVENT_CREATED",
        after={"name": name, "event_type": event_type, "status": event.status},
    )
    await write_outbox(
        session,
        aggregate_type="DR_EVENT",
        aggregate_id=event.id,
        event_type="EventStateChanged",
        payload={"status": event.status},
        clock=clock,
        dr_event_id=event.id,
    )
    return event
