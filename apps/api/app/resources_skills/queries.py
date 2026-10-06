"""Live resource read models (API_CONTRACT.md:91 `GET /teams/{id}/workload`, :143
`GET /dr-events/{id}/resources`; D-212; UI_UX.md "Resource sidebar"). Nothing is stored: every count is
taken from the Tasks at request time."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dr_events.participants import list_participant_user_ids, visible_event_ids_for_user
from app.dr_events.queries import live_event_ids
from app.tasks_dependencies.queries import OPEN_TASK_STATUSES, open_task_counts
from app.users_teams_org.authorization import AuthorizationService, Capability, Scope
from app.users_teams_org.queries import (
    active_teams_of,
    display_names,
    get_team,
    has_active_role,
    is_active_team_member,
    list_active_team_member_ids,
    skill_names_of,
)


def _zero() -> dict[str, int]:
    return dict.fromkeys(OPEN_TASK_STATUSES, 0)


@dataclass
class PersonLoad:
    user_id: uuid.UUID
    display_name: str
    counts: dict[str, int] = field(default_factory=_zero)
    teams: list[tuple[uuid.UUID, str]] = field(default_factory=list[tuple[uuid.UUID, str]])
    skills: list[str] = field(default_factory=list[str])
    is_manager: bool = False

    @property
    def open_total(self) -> int:
        return sum(self.counts.values())


@dataclass
class TeamLoad:
    team_id: uuid.UUID
    name: str
    counts: dict[str, int] = field(default_factory=_zero)
    unassigned_open: int = 0


@dataclass
class TeamWorkload:
    team: TeamLoad
    member_count: int
    members: list[PersonLoad]


@dataclass
class EventResources:
    dr_event_id: uuid.UUID
    people: list[PersonLoad]
    teams: list[TeamLoad]


async def _events_visible_to(session: AsyncSession, viewer_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Live Events the viewer may see, by `user_can_see_event`'s rule: every one for a Global Admin or a
    global read-only user (D-222), else only those they participate in -- a roll-up must not count work
    in Events they can't open."""
    events = live_event_ids()
    auth = AuthorizationService
    if await auth.is_global_admin(session, viewer_id) or await auth.is_global_readonly(session, viewer_id):
        return events
    return events.where(events.selected_columns[0].in_(visible_event_ids_for_user(viewer_id)))


async def may_view_team_workload(
    session: AsyncSession, viewer_id: uuid.UUID, team_id: uuid.UUID, *, at: datetime
) -> bool:
    """Admin / Coordinator, the Team's Manager, or an active member of it."""
    if await AuthorizationService.can(session, viewer_id, Capability.CROSS_TEAM_ASSIGNMENT, Scope()):
        return True
    team = await get_team(session, team_id)
    if (
        team is not None
        and team.manager_user_id == viewer_id
        and await has_active_role(session, viewer_id, "MANAGER")
    ):
        return True
    return await is_active_team_member(session, team_id, viewer_id, at=at)


async def team_workload(
    session: AsyncSession, *, viewer_id: uuid.UUID, team_id: uuid.UUID, at: datetime
) -> TeamWorkload | None:
    """Per member: open Tasks assigned to them (any Owning Team), by status. For the Team: open Tasks it
    owns, by status, and how many of those have nobody on them. Only live Events the viewer can see."""
    team = await get_team(session, team_id)
    if team is None:
        return None
    events = await _events_visible_to(session, viewer_id)
    member_ids = await list_active_team_member_ids(session, team_id, at=at)
    names = await display_names(session, member_ids)
    members = {
        m: PersonLoad(user_id=m, display_name=names.get(m, ""), is_manager=m == team.manager_user_id)
        for m in member_ids
    }
    for assignee, _team, status, n in await open_task_counts(
        session, event_ids=events, assignee_ids=member_ids
    ):
        if assignee is not None and assignee in members:
            members[assignee].counts[status] += n

    load = TeamLoad(team_id=team.id, name=team.name)
    for assignee, _team, status, n in await open_task_counts(
        session, event_ids=events, owning_team_id=team_id
    ):
        load.counts[status] += n
        if assignee is None:
            load.unassigned_open += n
    return TeamWorkload(
        team=load,
        member_count=len(member_ids),
        members=sorted(members.values(), key=lambda p: (p.display_name, str(p.user_id))),
    )


async def event_resources(session: AsyncSession, dr_event_id: uuid.UUID, *, at: datetime) -> EventResources:
    """The Event's participants with their Teams, skills and open-Task load *in this Event*, plus each
    Owning Team's open and unassigned work. No availability: nothing records it (BUILD-07.plan.md B5)."""
    rows = await open_task_counts(session, event_ids=[dr_event_id])
    people_ids = list(
        dict.fromkeys([*await list_participant_user_ids(session, dr_event_id), *(a for a, *_ in rows if a)])
    )
    names = await display_names(session, people_ids)
    teams_of = await active_teams_of(session, people_ids, at=at)
    skills = await skill_names_of(session, people_ids)
    people = {
        p: PersonLoad(user_id=p, display_name=names.get(p, ""), teams=teams_of[p], skills=skills[p])
        for p in people_ids
    }
    teams: dict[uuid.UUID, TeamLoad] = {}
    for assignee, owning_team_id, status, n in rows:
        if assignee is not None:
            people[assignee].counts[status] += n
        if owning_team_id not in teams:
            team = await get_team(session, owning_team_id)
            teams[owning_team_id] = TeamLoad(team_id=owning_team_id, name=team.name if team else "")
        teams[owning_team_id].counts[status] += n
        if assignee is None:
            teams[owning_team_id].unassigned_open += n
    return EventResources(
        dr_event_id=dr_event_id,
        people=sorted(people.values(), key=lambda p: (-p.open_total, p.display_name, str(p.user_id))),
        teams=sorted(teams.values(), key=lambda t: (t.name, str(t.team_id))),
    )
