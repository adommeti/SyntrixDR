from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.comments_mentions_notifications.mentions import MAX_COMMENT_CHARS, MAX_MENTIONS

CommentTargetType = Literal[
    "TASK", "DR_APPLICATION", "WORK_STREAM", "BLOCKER", "ISSUE_FINDING", "DR_EVENT", "MILESTONE"
]


class CommentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    author_user_id: uuid.UUID
    body: str
    parent_comment_id: uuid.UUID | None
    mentioned_user_ids: list[uuid.UUID]
    created_at: datetime


class CommentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comments: list[CommentResponse]


class CreateCommentRequest(BaseModel):
    """`POST /{target_type}/{target_id}/comments` (API_CONTRACT.md:217). Mentions: `@"Display Name"`,
    `@handle`, `@email` in the body, and/or explicit `mentioned_user_ids`."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MAX_COMMENT_CHARS, pattern=r"\S")
    parent_comment_id: uuid.UUID | None = None
    mentioned_user_ids: list[uuid.UUID] = Field(
        default=[], max_length=MAX_MENTIONS
    )  # pydantic copies defaults


class AlertResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    dr_event_id: uuid.UUID | None
    alert_type: str
    severity: str
    title: str
    body: str | None
    target_type: str | None
    target_id: uuid.UUID | None
    acknowledged_at: datetime | None
    acknowledged_by_user_id: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime


class AcknowledgeAlertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class SnoozeAlertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snoozed_until: AwareDatetime


class SnoozeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    alert_id: uuid.UUID
    user_id: uuid.UUID
    snoozed_until: datetime
