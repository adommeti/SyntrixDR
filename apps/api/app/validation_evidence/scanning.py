"""`MalwareScanner` port (ADR-024, D-245). Two adapters: `mock` (local/test) decides synchronously --
an EICAR test string anywhere in the payload is INFECTED, everything else CLEAN; `defender` (Azure)
never decides inline -- Microsoft Defender for Storage scans on upload and reports through
`POST /api/v1/internal/malware-result` (`webhook.py`), so it answers PENDING. Neither adapter is a
required dependency of a critical workflow: a PENDING file simply doesn't count yet (invariant 14)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Protocol

from fastapi import Depends, Request

from app.core.config import get_settings


class MalwareScanStatus(StrEnum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"


#: The EICAR anti-malware test file (the standard, harmless detection string).
EICAR_SIGNATURE = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class MalwareScanner(Protocol):
    name: str

    async def scan(self, content: bytes, *, file_name: str) -> MalwareScanStatus: ...


class MockMalwareScanner:
    """Local and CI adapter: EICAR payload -> INFECTED, else CLEAN (D-245)."""

    name = "mock"

    async def scan(self, content: bytes, *, file_name: str) -> MalwareScanStatus:
        _ = file_name
        return MalwareScanStatus.INFECTED if EICAR_SIGNATURE in content else MalwareScanStatus.CLEAN


class DefenderMalwareScanner:
    """Azure adapter stub: the verdict arrives out of band (Event Grid -> webhook), so an upload is
    PENDING until then. The webhook receiver is the other half (`webhook.py`)."""

    name = "defender"

    async def scan(self, content: bytes, *, file_name: str) -> MalwareScanStatus:
        _ = (content, file_name)
        return MalwareScanStatus.PENDING


def scanner_from_settings(kind: str) -> MalwareScanner:
    if kind == "defender":
        return DefenderMalwareScanner()
    if kind == "mock":
        return MockMalwareScanner()
    raise ValueError(f"unknown malware_scanner {kind!r} (expected 'mock' or 'defender')")


async def get_malware_scanner(request: Request) -> MalwareScanner:
    """FastAPI dependency: the process-wide adapter on `app.state`, chosen by `malware_scanner`."""
    scanner = getattr(request.app.state, "malware_scanner", None)
    if scanner is None:
        scanner = scanner_from_settings(get_settings().malware_scanner)
        request.app.state.malware_scanner = scanner
    return scanner  # pyright: ignore[reportReturnType]


MalwareScannerDep = Annotated[MalwareScanner, Depends(get_malware_scanner)]
