from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.core.errors import AppError


class InvalidWorkbookError(AppError):
    code = "IMPORT_FILE_UNREADABLE"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("The uploaded file could not be read as an Excel workbook.")


@dataclass(frozen=True)
class ParsedRow:
    #: 1-indexed row number in the original worksheet (header is row 1, so the first data row is
    #: 2) -- this is D-221's `source_import_row` provenance value.
    row_number: int
    values: dict[str, Any]


def parse_workbook(data: bytes) -> tuple[list[str], list[ParsedRow]]:
    """No rigid Excel format (D-221): reads whatever headers the first row of the active sheet
    has, arbitrary order/naming. Raises `InvalidWorkbookError` for an unreadable file or a sheet
    with no header row -- never partially parses a corrupt file."""
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (InvalidFileException, OSError, KeyError, zipfile.BadZipFile) as exc:
        raise InvalidWorkbookError() from exc

    sheet = workbook.active
    if sheet is None:
        raise InvalidWorkbookError()

    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration as exc:
        raise InvalidWorkbookError() from exc

    headers = [str(cell).strip() for cell in header_row if cell is not None and str(cell).strip()]
    header_indexes = [i for i, cell in enumerate(header_row) if cell is not None and str(cell).strip()]

    parsed_rows: list[ParsedRow] = []
    for row_number, row in enumerate(rows_iter, start=2):
        if all(cell is None for cell in row):
            continue
        values = {
            header: (row[i] if i < len(row) else None)
            for header, i in zip(headers, header_indexes, strict=False)
        }
        parsed_rows.append(ParsedRow(row_number=row_number, values=values))

    return headers, parsed_rows
