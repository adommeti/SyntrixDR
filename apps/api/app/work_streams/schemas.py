from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: D-227 (`work_stream_type`, schema_v2_reconciliation.sql §3).
StreamType = Literal["NETWORK", "STORAGE", "DATABASE", "APPLICATIONS", "MONITORING", "VALIDATION", "CUSTOM"]


class CreateWorkStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    stream_type: StreamType = "CUSTOM"
    description: str | None = None
    lead_user_id: uuid.UUID | None = None
    owning_team_id: uuid.UUID | None = None
    # INTEGER column: out of range is a 422 here, not a DataError (500) at flush.
    sequence_order: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1)


class WorkStreamResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    dr_event_id: uuid.UUID
    name: str
    description: str | None
    stream_type: StreamType
    lead_user_id: uuid.UUID | None
    owning_team_id: uuid.UUID | None
    sequence_order: int | None
    version: int
    created_at: datetime
    updated_at: datetime


class WorkStreamListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_streams: list[WorkStreamResponse]
