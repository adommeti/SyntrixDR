"""schema_v1 (historical basis, D-247)

Revision ID: 0001_schema_v1
Revises:
Create Date: 2026-09-12

"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision: str = "0001_schema_v1"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_SPEC_DIR = Path(__file__).resolve().parents[4] / "docs" / "spec"

# schema_v1.sql creation order (for a clean, dependency-safe downgrade).
_TABLES_IN_ORDER = [
    "dr_health_snapshots",
    "plan_version_milestones",
    "plan_version_task_dependencies",
    "plan_version_tasks",
    "import_jobs",
    "document_chunks",
    "documents",
    "external_references",
    "export_packages",
    "report_versions",
    "reports",
    "needs_review_items",
    "audit_events",
    "overrides",
    "policy_values",
    "policy_definitions",
    "saved_views",
    "task_labels",
    "labels",
    "alert_snoozes",
    "notifications",
    "alerts",
    "mentions",
    "comments",
    "evidence_items",
    "validations",
    "issue_findings",
    "blockers",
    "milestone_dependencies",
    "milestone_tasks",
    "milestones",
    "task_dependencies",
    "tasks",
    "work_streams",
    "dr_applications",
    "plan_versions",
    "dr_events",
    "plans",
    "application_owners",
    "applications",
    "tiers",
    "role_assignments",
    "user_skills",
    "skills",
    "team_memberships",
    "teams",
    "users",
]

_ENUM_TYPES_IN_ORDER = [
    "health_band",
    "review_status",
    "review_source",
    "report_status",
    "ai_control_profile",
    "saved_view_visibility",
    "evidence_type",
    "milestone_confirmation_mode",
    "milestone_status",
    "validation_status",
    "validation_target_type",
    "blocker_status",
    "dependency_strength",
    "dependency_type",
    "task_status",
    "task_phase",
    "plan_version_type",
    "plan_source_type",
    "plan_type",
    "dr_application_status",
    "dr_event_status",
    "dr_event_type",
    "role_scope_type",
    "identity_type",
    "application_owner_type",
    "issue_finding_status",
    "issue_finding_type",
]


def upgrade() -> None:
    sql = (_SPEC_DIR / "schema_v1.sql").read_text()
    op.execute(sql)


def downgrade() -> None:
    for table in _TABLES_IN_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_type in _ENUM_TYPES_IN_ORDER:
        op.execute(f"DROP TYPE IF EXISTS {enum_type} CASCADE")
