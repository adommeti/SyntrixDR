from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TierResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    code: str
    rank: int
    default_sla_minutes: int
    default_health_weight: float
    description: str | None
    version: int


class TierListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: list[TierResponse]


class TierUpdateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    expected_version: int
    default_sla_minutes: int
    default_health_weight: Decimal
    description: str | None = None


class UpdateTiersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: list[TierUpdateItem]


class ApplicationOwnerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_type: str
    owner_order: int = Field(ge=1, le=3)
    user_id: uuid.UUID


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    description: str | None
    tier_id: uuid.UUID
    external_system: str | None
    external_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    owners: list[ApplicationOwnerResponse] = []


class ApplicationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applications: list[ApplicationResponse]


class CreateApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    tier_id: uuid.UUID
    description: str | None = None
    external_system: str | None = None
    external_id: str | None = None


class UpdateApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int
    name: str | None = None
    description: str | None = None
    tier_id: uuid.UUID | None = None
    external_system: str | None = None
    external_id: str | None = None


class SetApplicationOwnerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_type: str
    owner_order: int = Field(ge=1, le=3)
    user_id: uuid.UUID


class SetApplicationOwnersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owners: list[SetApplicationOwnerItem]


class ApplicationHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, object]] = []
