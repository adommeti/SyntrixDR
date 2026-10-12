from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EvidenceType = Literal["TEXT", "LOG", "EXTERNAL_LINK", "FILE", "SCREENSHOT"]
EvidenceTargetType = Literal["TASK", "DR_APPLICATION", "MILESTONE", "BLOCKER", "VALIDATION", "ISSUE_FINDING"]
ScanStatus = Literal["PENDING", "CLEAN", "INFECTED"]


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    evidence_type: EvidenceType
    text_content: str | None
    external_url: str | None
    file_name: str | None
    mime_type: str | None
    size_bytes: int | None
    checksum: str | None
    malware_scan_status: ScanStatus | None
    malware_scanned_at: datetime | None
    file_attached: bool
    download_url: str | None  # a short-lived SAS URL, only when the file is CLEAN (ADR-014, D-245)
    uploaded_by_user_id: uuid.UUID
    created_at: datetime


class CreateEvidenceRequest(BaseModel):
    """`POST /evidence` (API_CONTRACT.md:203): text / link evidence, or the file-upload handshake."""

    model_config = ConfigDict(extra="forbid")

    target_type: EvidenceTargetType
    target_id: uuid.UUID
    evidence_type: EvidenceType = "TEXT"
    text_content: str | None = Field(default=None, max_length=50_000)
    external_url: str | None = Field(default=None, max_length=2_048)
    file_name: str | None = Field(default=None, max_length=255)
