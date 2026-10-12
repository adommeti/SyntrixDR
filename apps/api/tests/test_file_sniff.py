"""Content sniffing for the D-225 allowlist: the bytes must be what the extension claims."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.validation_evidence.sniff import CANONICAL_MIME, extension_of, sniff_matches, sniffed_mime

pytestmark = [pytest.mark.files]


def _zip(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "x")
    return buffer.getvalue()


PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
XLSX = _zip("[Content_Types].xml", "xl/workbook.xml")
DOCX = _zip("[Content_Types].xml", "word/document.xml")
ZIP = _zip("readme.txt")


@pytest.mark.parametrize(
    ("content", "extension", "ok"),
    [
        (PDF, "pdf", True),
        (PNG, "png", True),
        (JPG, "jpg", True),
        (JPG, "jpeg", True),
        (b"hello\nworld\n", "txt", True),
        (b"2026-10-11 INFO ok\n", "log", True),
        (b"a,b\n1,2\n", "csv", True),
        (json.dumps({"a": 1}).encode(), "json", True),
        (XLSX, "xlsx", True),
        (DOCX, "docx", True),
        (ZIP, "zip", True),
        (XLSX, "zip", True),  # an Office file is a valid zip
        # mismatches
        (PNG, "pdf", False),  # PNG bytes named .pdf
        (PDF, "png", False),
        (b"hello\x00world", "txt", False),  # NUL byte: not text
        (b"\xff\xfe\xfd", "log", False),  # not UTF-8
        (b"{not json", "json", False),
        (b"plain text", "json", False),
        (ZIP, "xlsx", False),  # a plain zip is not a workbook
        (DOCX, "xlsx", False),
        (XLSX, "docx", False),
        (b"PK\x03\x04garbage", "zip", False),  # bad zip
        (b"", "txt", False),  # empty never matches
        (PDF, "exe", False),  # not on the list at all
        (PDF, "", False),
    ],
)
def test_sniff_matches(content: bytes, extension: str, ok: bool) -> None:
    assert sniff_matches(content, extension) is ok
    assert sniffed_mime(content, extension) == (CANONICAL_MIME[extension] if ok else None)


def test_extension_of() -> None:
    assert extension_of("Report.Final.PDF") == "pdf"
    assert extension_of("noext") == ""
    assert extension_of(".hidden") == "hidden"


def test_every_allowlisted_extension_has_a_canonical_mime() -> None:
    assert set(CANONICAL_MIME) == {
        "pdf",
        "png",
        "jpg",
        "jpeg",
        "txt",
        "log",
        "csv",
        "xlsx",
        "docx",
        "zip",
        "json",
    }


def test_deeply_nested_json_is_a_mismatch_not_a_crash() -> None:
    assert sniff_matches(b"[" * 100_000, "json") is False
