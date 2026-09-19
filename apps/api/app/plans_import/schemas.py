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


class ImportJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    dr_event_id: uuid.UUID
    status: str
    source_file_uri: str
    error_data: dict[str, object] | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ColumnMappingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_header: str
    target_field: str | None
    confidence: float


class ImportRowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_number: int
    values: dict[str, object]


class ImportJobDetailResponse(ImportJobResponse):
    headers: list[str] = []
    proposed_mapping: list[ColumnMappingResponse] = []
    rows: list[ImportRowResponse] = []


class ColumnMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_header: str
    target_field: str | None = None


class AcceptImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping: list[ColumnMappingRequest]


class AcceptImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_job: ImportJobResponse
    created_task_count: int
    created_dependency_count: int
    needs_review_count: int
