from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

#: schema_v1.sql `milestone_status` / `milestone_confirmation_mode`.
MilestoneStatus = Literal[
    "NOT_STARTED", "IN_PROGRESS", "AT_RISK", "READY_FOR_CONFIRMATION", "ACHIEVED", "MISSED"
]
ConfirmationMode = Literal["MANUAL", "AUTOMATIC"]
DependencyStrength = Literal["HARD", "ADVISORY"]


class MilestoneContributionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    is_required: bool = True


class MilestoneGateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    strength: DependencyStrength = "HARD"


class CreateMilestoneRequest(BaseModel):
    """`POST /dr-events/{id}/milestones` (API_CONTRACT.md:150). Contributions and gates are set here;
    the contract has no Milestone edit route (BUILD-07.plan.md Risk #6)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, pattern=r"\S")
    description: str | None = None
    work_stream_id: uuid.UUID | None = None
    dr_application_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    confirmation_mode: ConfirmationMode = "MANUAL"  # critical Milestones are manual by default (§7.13)
    target_at: AwareDatetime | None = None
    contributing_tasks: list[MilestoneContributionIn] = []  # pydantic copies mutable defaults
    gates: list[MilestoneGateIn] = []

    @field_validator("contributing_tasks", "gates")
    @classmethod
    def _one_row_per_task(cls, rows: list[MilestoneContributionIn] | list[MilestoneGateIn]) -> object:
        task_ids = [row.task_id for row in rows]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("each task_id may appear once")
        return rows


class ConfirmMilestoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int


class MilestoneContributionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    is_required: bool


class MilestoneGateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    milestone_dependency_id: uuid.UUID
    task_id: uuid.UUID
    strength: DependencyStrength


class MilestoneResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    dr_event_id: uuid.UUID
    work_stream_id: uuid.UUID | None
    dr_application_id: uuid.UUID | None
    name: str
    description: str | None
    status: MilestoneStatus
    owner_user_id: uuid.UUID | None
    confirmation_mode: ConfirmationMode
    ready_for_confirmation_at: datetime | None
    achieved_at: datetime | None
    target_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    contributing_tasks: list[MilestoneContributionResponse]
    gates: list[MilestoneGateResponse]


class MilestoneListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    milestones: list[MilestoneResponse]
