from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SnapshotItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID | None = None
    snapshot_data: dict[str, object]


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    description: str | None
    plan_type: str
    application_id: uuid.UUID | None
    source_type: str
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class PlanListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plans: list[PlanResponse]


class CreatePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    plan_type: str
    description: str | None = None
    application_id: uuid.UUID | None = None


class PlanVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    plan_id: uuid.UUID | None
    dr_event_id: uuid.UUID | None
    version_number: int
    version_type: str
    notes: str | None
    created_at: datetime
    task_count: int
    task_dependency_count: int
    milestone_count: int


class PlanDetailResponse(PlanResponse):
    versions: list[PlanVersionResponse] = []


class CreatePlanVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_type: str
    notes: str | None = None
    tasks: list[SnapshotItemRequest] = []
    task_dependencies: list[SnapshotItemRequest] = []
    milestones: list[SnapshotItemRequest] = []
