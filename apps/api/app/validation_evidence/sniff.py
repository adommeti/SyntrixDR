"""Server-side content sniffing for the D-225 allowlist (SECURITY_REVIEW.md:107 "do not trust client
MIME/filename"). Pure functions, no libmagic: magic numbers for pdf/png/jpeg, ZIP-container inspection
for zip/xlsx/docx, UTF-8-without-NUL for txt/log/csv, a JSON parse for json. The sniffed MIME is what
gets stored and what is checked against `file_policy.allowed_mime_types`; the client's `Content-Type`
is ignored (BUILD-09.plan.md Risk #2)."""

from __future__ import annotations

import io
import json
import zipfile

#: Extension -> the MIME this sniffer reports when the bytes match.
CANONICAL_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "txt": "text/plain",
    "log": "text/plain",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "zip": "application/zip",
    "json": "application/json",
}


def extension_of(file_name: str) -> str:
    return file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""


def _is_text(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _zip_kind(content: bytes) -> str | None:
    """'xlsx' / 'docx' / 'zip' for a ZIP container, by its Office part names; None if not a ZIP."""
    if not content.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return None
    if "[Content_Types].xml" in names:
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
    return "zip"


def sniff_matches(content: bytes, extension: str) -> bool:
    """True when the bytes are what `extension` claims. An empty file never matches anything."""
    if not content:
        return False
    match extension:
        case "pdf":
            return content.startswith(b"%PDF-")
        case "png":
            return content.startswith(b"\x89PNG\r\n\x1a\n")
        case "jpg" | "jpeg":
            return content.startswith(b"\xff\xd8\xff")
        case "txt" | "log" | "csv":
            return _is_text(content)
        case "json":
            if not _is_text(content):
                return False
            try:
                json.loads(content.decode("utf-8"))
            except ValueError:
                return False
            return True
        case "xlsx" | "docx":
            return _zip_kind(content) == extension
        case "zip":
            return _zip_kind(content) is not None  # an Office file is still a valid zip
        case _:
            return False


def sniffed_mime(content: bytes, extension: str) -> str | None:
    """The canonical MIME for `extension` when the bytes match it, else None."""
    return CANONICAL_MIME.get(extension) if sniff_matches(content, extension) else None
