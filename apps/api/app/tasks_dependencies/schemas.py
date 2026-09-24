from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
    phase: str
    status: str
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
