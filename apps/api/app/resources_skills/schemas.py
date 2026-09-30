from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class AssignTaskRequest(BaseModel):
    """`POST /tasks/{id}/assign` (API_CONTRACT.md:165). No unassign: the contract only assigns
    (BUILD-07.plan.md Risk B4)."""

    model_config = ConfigDict(extra="forbid")

    assignee_user_id: uuid.UUID
    expected_version: int


class VolunteerTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int


class OpenTaskCounts(BaseModel):
    """Open (non-terminal) Tasks by status; `Ready` is derived and never counted as a status."""

    model_config = ConfigDict(extra="forbid")

    NOT_STARTED: int
    IN_PROGRESS: int
    BLOCKED: int
    READY_FOR_VALIDATION: int


class TeamRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: uuid.UUID
    name: str


class PersonLoadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    display_name: str
    open_tasks: OpenTaskCounts
    open_total: int


class TeamMemberLoadResponse(PersonLoadResponse):
    is_manager: bool


class EventPersonResponse(PersonLoadResponse):
    teams: list[TeamRef]
    skills: list[str]


class TeamLoadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: uuid.UUID
    name: str
    open_tasks: OpenTaskCounts
    unassigned_open: int


class TeamWorkloadResponse(BaseModel):
    """`GET /teams/{id}/workload` (API_CONTRACT.md:91). Keeps BUILD-02's `team_id`/`team_name`/
    `member_count` and adds the live roll-up."""

    model_config = ConfigDict(extra="forbid")

    team_id: uuid.UUID
    team_name: str
    member_count: int
    owned_open_tasks: OpenTaskCounts
    unassigned_open: int
    members: list[TeamMemberLoadResponse]


class EventResourcesResponse(BaseModel):
    """`GET /dr-events/{id}/resources` (API_CONTRACT.md:143, D-212)."""

    model_config = ConfigDict(extra="forbid")

    dr_event_id: uuid.UUID
    people: list[EventPersonResponse]
    teams: list[TeamLoadResponse]
