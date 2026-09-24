from __future__ import annotations

import uuid
from datetime import datetime

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
