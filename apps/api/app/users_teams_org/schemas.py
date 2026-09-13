from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    display_name: str
    email: str
    identity_type: str
    job_title: str | None
    is_active: bool


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    display_name: str
    email: str
    identity_type: str
    job_title: str | None
    avatar_uri: str | None
    is_active: bool


class TeamResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    description: str | None
    manager_user_id: uuid.UUID | None


class TeamListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teams: list[TeamResponse]


class TeamWorkloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: uuid.UUID
    team_name: str
    member_count: int


class CreateLocalUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    email: str
    password: str


class CreateLocalUserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
