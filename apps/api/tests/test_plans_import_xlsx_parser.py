"""Unit tests for BUILD-05's `xlsx_parser.py`. Pure functions, no DB -- builds workbooks in
memory with openpyxl rather than reading fixture files (those are exercised by the end-to-end
`test_plans_import_excel.py` instead)."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.plans_import.xlsx_parser import InvalidWorkbookError, parse_workbook


def _workbook_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_parses_headers_and_rows_with_correct_row_numbers() -> None:
    data = _workbook_bytes(
        [
            ["Task", "Owning Team"],
            ["Repoint DNS", "Network"],
            ["Restore Database", "DBA"],
        ]
    )

    headers, rows = parse_workbook(data)

    assert headers == ["Task", "Owning Team"]
    assert [r.row_number for r in rows] == [2, 3]
    assert rows[0].values == {"Task": "Repoint DNS", "Owning Team": "Network"}
    assert rows[1].values == {"Task": "Restore Database", "Owning Team": "DBA"}


def test_skips_fully_blank_rows() -> None:
    data = _workbook_bytes(
        [
            ["Task"],
            ["Repoint DNS"],
            [None],
            ["Restore Database"],
        ]
    )

    _headers, rows = parse_workbook(data)

    assert [r.row_number for r in rows] == [2, 4]


def test_ignores_columns_with_no_header() -> None:
    data = _workbook_bytes(
        [
            ["Task", None, "Notes"],
            ["Repoint DNS", "stray value", "first"],
        ]
    )

    headers, rows = parse_workbook(data)

    assert headers == ["Task", "Notes"]
    assert rows[0].values == {"Task": "Repoint DNS", "Notes": "first"}


def test_corrupt_file_raises_invalid_workbook_error() -> None:
    with pytest.raises(InvalidWorkbookError):
        parse_workbook(b"not a real xlsx file")


def test_empty_sheet_raises_invalid_workbook_error() -> None:
    data = _workbook_bytes([])

    with pytest.raises(InvalidWorkbookError):
        parse_workbook(data)
