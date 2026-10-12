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


class FilePolicyResponse(BaseModel):
    """`GET/PUT /admin/file-policy` (API_CONTRACT.md:266)."""

    model_config = ConfigDict(extra="forbid")

    max_mb: int
    max_bytes: int
    allowed_extensions: list[str]
    allowed_mime_types: list[str]
    scanning_required: bool
    updated_by_user_id: uuid.UUID | None


class UpdateFilePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_mb: int
    allowed_extensions: list[str]
    scanning_required: bool = True
