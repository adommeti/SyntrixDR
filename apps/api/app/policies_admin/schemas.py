from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict


class PolicyItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    description: str | None
    value_type: str
    value: Any


class PolicyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PolicyItemResponse]


class SetPolicyValueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any
    scope_type: str
    scope_id: uuid.UUID | None = None


class PolicyValueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    key: str
    value: Any
    scope_type: str
    scope_id: uuid.UUID | None
