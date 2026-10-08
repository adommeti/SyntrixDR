from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

#: schema_v1.sql `blocker_status`.
BlockerStatus = Literal["OPEN", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "VERIFIED", "CLOSED"]


class BlockerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    reason: str
    category: str | None
    status: BlockerStatus
    blocker_team_id: uuid.UUID | None
    blocker_owner_user_id: uuid.UUID | None
    blocked_at: datetime
    claimed_at: datetime | None
    resolved_at: datetime | None
    verified_at: datetime | None
    closed_at: datetime | None
    resolution_note: str | None
    version: int
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class BlockerListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blockers: list[BlockerResponse]


class AssignBlockerRequest(BaseModel):
    """`POST /blockers/{id}/assign` (API_CONTRACT.md:181): route to a Team queue and/or a resolver."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int
    team_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None


class StartBlockerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int


class ResolveBlockerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    resolution_note: str | None = None


class VerifyBlockerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
