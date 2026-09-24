"""D-216: the canonical polymorphic `target_type` enum, defined once. `packages/contracts`
`TARGET_TYPES` mirrors it (D-234) and `tests/test_target_types.py` pins the two to the live database
enum and its per-table CHECKs."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM as PgEnum


class TargetType(StrEnum):
    DR_EVENT = "DR_EVENT"
    DR_APPLICATION = "DR_APPLICATION"
    APPLICATION = "APPLICATION"
    WORK_STREAM = "WORK_STREAM"
    TASK = "TASK"
    TASK_DEPENDENCY = "TASK_DEPENDENCY"
    MILESTONE = "MILESTONE"
    BLOCKER = "BLOCKER"
    ISSUE_FINDING = "ISSUE_FINDING"
    VALIDATION = "VALIDATION"
    IMPORT_JOB = "IMPORT_JOB"
    PLAN = "PLAN"
    PLAN_VERSION = "PLAN_VERSION"
    REPORT = "REPORT"
    DOCUMENT = "DOCUMENT"
    ALERT = "ALERT"


#: The one column type for every `target_type`-typed column. `create_type=False`: migration 0002
#: owns the database type (migrations rule).
target_type_enum = PgEnum(*(t.value for t in TargetType), name="target_type", create_type=False)

#: Tables that accept only a subset, mirroring 0002's CHECKs (schema_v2_reconciliation.sql:141-155).
#: overrides, alerts and notifications take any label and are deliberately absent. Endpoints that let
#: a client name a target (comments, evidence -- BUILD-08/09) validate against this before insert.
TARGET_TYPE_SUBSETS: dict[str, frozenset[TargetType]] = {
    "comments": frozenset(
        {
            TargetType.TASK,
            TargetType.DR_APPLICATION,
            TargetType.WORK_STREAM,
            TargetType.BLOCKER,
            TargetType.ISSUE_FINDING,
            TargetType.DR_EVENT,
            TargetType.MILESTONE,
        }
    ),
    "evidence_items": frozenset(
        {
            TargetType.TASK,
            TargetType.DR_APPLICATION,
            TargetType.MILESTONE,
            TargetType.BLOCKER,
            TargetType.VALIDATION,
            TargetType.ISSUE_FINDING,
        }
    ),
    "needs_review_items": frozenset(
        {
            TargetType.TASK,
            TargetType.TASK_DEPENDENCY,
            TargetType.DR_APPLICATION,
            TargetType.IMPORT_JOB,
            TargetType.DOCUMENT,
            TargetType.MILESTONE,
        }
    ),
}
