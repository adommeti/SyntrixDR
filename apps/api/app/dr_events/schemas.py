from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class DrEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    parent_dr_event_id: uuid.UUID | None
    name: str
    event_type: str
    description: str | None
    status: str
    coordinator_user_id: uuid.UUID | None
    source_location: str | None
    target_location: str | None
    planned_start_at: datetime | None
    event_timezone: str
    network_cut_at: datetime | None
    failback_started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    ai_control_profile: str
    baseline_plan_version_id: uuid.UUID | None
    health_score: Decimal | None
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class DrEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[DrEventResponse]


class CreateDrEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    event_type: str
    description: str | None = None
    application_ids: list[uuid.UUID] = []
    plan_id: uuid.UUID | None = None
    plan_version_id: uuid.UUID | None = None
    parent_dr_event_id: uuid.UUID | None = None


class ExpectedVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int


class StartFailoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    network_cut_at: datetime | None = None


class CancelEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    reason: str
