from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

IssueType = Literal["ISSUE", "FINDING", "RISK", "DEVIATION", "LESSON"]
IssueStatus = Literal["OPEN", "IN_REVIEW", "RESOLVED", "DISMISSED"]


class IssueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    dr_event_id: uuid.UUID
    dr_application_id: uuid.UUID | None
    work_stream_id: uuid.UUID | None
    task_id: uuid.UUID | None
    item_type: IssueType
    status: IssueStatus
    title: str
    description: str
    priority: str | None
    owner_user_id: uuid.UUID | None
    resolution_note: str | None
    version: int
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class IssueListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[IssueResponse]


class CreateIssueRequest(BaseModel):
    """`POST /dr-events/{id}/issues` (API_CONTRACT.md:185)."""

    model_config = ConfigDict(extra="forbid")

    item_type: IssueType = "ISSUE"
    title: str = Field(min_length=1, pattern=r"\S")
    description: str = Field(min_length=1, pattern=r"\S")
    priority: str | None = None
    dr_application_id: uuid.UUID | None = None
    work_stream_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None


class ReviewIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int


class CloseIssueRequest(BaseModel):
    """`resolve` / `dismiss` -- the note is required by the service (422 `RESOLUTION_NOTE_REQUIRED`)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int
    note: str | None = None
