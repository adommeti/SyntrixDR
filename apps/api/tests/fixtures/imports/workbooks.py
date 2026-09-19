"""Three D-236-compliant (fictitious data only) `.xlsx` header-variant fixture workbooks for
BUILD-05's Excel import tests. Built in-memory with openpyxl rather than committed as binary
files -- reviewable diffs, always in sync with the column names `mapping.py` actually expects."""

from __future__ import annotations

import io

from openpyxl import Workbook


def _to_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def canonical_workbook() -> bytes:
    """D-221's exact fixture header list, all mapped, all resolvable."""
    return _to_bytes(
        [
            [
                "Phase",
                "Work Stream",
                "Application",
                "Tier",
                "Task",
                "Subtask / Procedure",
                "Owning Team",
                "Assignee",
                "Expected Duration",
                "Predecessor / Dependency",
                "Failover / Failback",
                "Evidence Required",
                "Notes",
            ],
            [
                "Failover",
                "Network",
                "Billing Service",
                "TIER_1",
                "Repoint DNS",
                "",
                "Network Team",
                "Alex Rivera",
                "30",
                "",
                "Failover",
                "Yes",
                "Cut over the public DNS record.",
            ],
            [
                "Failover",
                "Database",
                "Billing Service",
                "TIER_1",
                "Restore Database",
                "",
                "DBA Team",
                "",
                "2h",
                "Repoint DNS",
                "Failover",
                "Yes",
                "Restore from the latest replica.",
            ],
        ]
    )


def reordered_renamed_workbook() -> bytes:
    """Same semantic content as `canonical_workbook`, but with reordered, renamed headers --
    proves the mapper is header-order/naming independent (D-221: no rigid Excel format)."""
    return _to_bytes(
        [
            ["Task Title", "Team", "Duration (min)", "Notes", "Work Stream"],
            ["Repoint DNS", "Network Team", "30", "Cut over the public DNS record.", "Network"],
            ["Restore Database", "DBA Team", "120", "Restore from the latest replica.", "Database"],
        ]
    )


def messy_workbook() -> bytes:
    """Deliberately exercises every `needs_review_items` path from BUILD-05.plan.md Risk #4: a
    row with no Task title, a row with an unresolvable Owning Team, and a row referencing an
    unresolved predecessor."""
    return _to_bytes(
        [
            ["Task", "Owning Team", "Work Stream", "Predecessor / Dependency", "Notes"],
            ["", "Network Team", "Network", "", "Row with no title -- must be skipped, flagged"],
            [
                "Orphaned Task",
                "Nonexistent Team",
                "",
                "",
                "Row with an unresolvable Owning Team -- must be skipped, flagged",
            ],
            [
                "Verify Failover",
                "Network Team",
                "Network",
                "Some Task That Was Never Imported",
                "References a predecessor that doesn't exist in this workbook",
            ],
        ]
    )


def cyclical_workbook() -> bytes:
    """Two rows whose predecessor references point at each other -- Row A's predecessor is Row B,
    and Row B's predecessor is Row A -- proves the accept flow rejects (flags for review, doesn't
    silently commit) a directed dependency cycle rather than writing one into the live Task graph."""
    return _to_bytes(
        [
            ["Task", "Owning Team", "Work Stream", "Predecessor / Dependency", "Notes"],
            ["Task A", "Network Team", "Network", "Task B", "Predecessor is Task B"],
            ["Task B", "Network Team", "Network", "Task A", "Predecessor is Task A"],
        ]
    )
