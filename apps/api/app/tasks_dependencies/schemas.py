from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: schema_v1.sql `task_phase` / `task_status` (STATE_MACHINES.md §Task, D-252).
TaskPhase = Literal["PRE_DR", "FAILOVER", "VALIDATION", "FAILBACK", "POST_DR"]
#: Column bounds (`expected_duration_minutes`/`sort_order` INTEGER, `evidence_min_count` SMALLINT), so an
#: out-of-range value is a 422 here rather than a DataError (500) at flush.
_INT4_MAX = 2_147_483_647
_INT2_MAX = 32_767
TaskStatus = Literal[
    "NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_FOR_VALIDATION", "COMPLETED", "CANCELLED"
]

# Reasons and notes are `str | None` here on purpose: the service decides whether one is required
# and rejects blank ones with an API_CONTRACT error code. A required schema field would fail as
# FastAPI's bare 422 instead, outside the error envelope (BUILD-06.plan.md, Commands section).


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    dr_event_id: uuid.UUID
    dr_application_id: uuid.UUID | None
    work_stream_id: uuid.UUID | None
    parent_task_id: uuid.UUID | None
    title: str
    description: str | None
    phase: TaskPhase
    status: TaskStatus
    owning_team_id: uuid.UUID
    current_assignee_user_id: uuid.UUID | None
    expected_duration_minutes: int | None
    started_at: datetime | None
    completed_at: datetime | None
    needs_specific_validation: bool
    evidence_required: bool
    evidence_min_count: int
    verification_note_required: bool
    source_import_id: uuid.UUID | None
    source_import_row: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class StartTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    override_reason: str | None = None


class BlockTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    reason: str | None = None


class ResumeTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    override_reason: str | None = None


class SubmitValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    verification_note: str | None = None


class ValidateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    approve: bool
    note: str | None = None


class CancelTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    reason: str | None = None


class CreateTaskDependencyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predecessor_task_id: uuid.UUID
    successor_task_id: uuid.UUID
    strength: Literal["HARD", "ADVISORY"] = "HARD"


class TaskDependencyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    dr_event_id: uuid.UUID
    predecessor_task_id: uuid.UUID
    successor_task_id: uuid.UUID
    dependency_type: str
    strength: str
    created_by_user_id: uuid.UUID
    created_at: datetime
    deleted_at: datetime | None


class GraphNodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    kind: Literal["TASK", "MILESTONE"]
    label: str
    status: str
    dr_application_id: uuid.UUID | None
    work_stream_id: uuid.UUID | None
    ready: bool | None
    blocked_by: list[uuid.UUID]
    advisory_pending: list[uuid.UUID]
    active_blocker_count: int


class GraphEdgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    kind: Literal["TASK_DEPENDENCY", "MILESTONE_GATE"]
    from_id: uuid.UUID
    to_id: uuid.UUID
    strength: Literal["HARD", "ADVISORY"]


class BlockedPathResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    root_task_id: uuid.UUID
    downstream_task_ids: list[uuid.UUID]
    impacted_dr_application_ids: list[uuid.UUID]


class DependencyGraphResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    dr_event_id: uuid.UUID
    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
    blocked_paths: list[BlockedPathResponse]


class CreateTaskRequest(BaseModel):
    """API_CONTRACT.md:157 -- the body carries the D-226 evidence fields and D-209's
    `needs_specific_validation`, with their schema defaults."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    phase: TaskPhase
    owning_team_id: uuid.UUID
    dr_application_id: uuid.UUID | None = None
    work_stream_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    description: str | None = None
    expected_duration_minutes: int | None = Field(default=None, ge=0, le=_INT4_MAX)
    sort_order: int | None = Field(default=None, ge=-_INT4_MAX - 1, le=_INT4_MAX)
    evidence_required: bool = True
    evidence_min_count: int = Field(default=1, ge=0, le=_INT2_MAX)
    verification_note_required: bool = True
    needs_specific_validation: bool = False


class UpdateTaskRequest(BaseModel):
    """`PATCH /tasks/{id}` (API_CONTRACT.md:158). Only fields present in the body change
    (`model_fields_set`); the defaults below are never applied. `extra="forbid"` is what keeps status,
    Owning Team, assignee, context, phase and parent out (422)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int
    title: str = Field(default="", min_length=1, pattern=r"\S")
    description: str | None = None
    expected_duration_minutes: int | None = Field(default=None, ge=0, le=_INT4_MAX)
    sort_order: int | None = Field(default=None, ge=-_INT4_MAX - 1, le=_INT4_MAX)
    evidence_required: bool = True
    evidence_min_count: int = Field(default=1, ge=0, le=_INT2_MAX)
    verification_note_required: bool = True
    needs_specific_validation: bool = False


class TaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskResponse]
