"""schema_v2 reconciliation (D-247)

Revision ID: 0002_reconciliation
Revises: 0001_schema_v1
Create Date: 2026-09-12

"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision: str = "0002_reconciliation"
down_revision: str | None = "0001_schema_v1"
branch_labels: str | None = None
depends_on: str | None = None

_SPEC_DIR = Path(__file__).resolve().parents[4] / "docs" / "spec"

_SEEDED_POLICY_KEYS = [
    "sla.warning_percent",
    "sla.warning_minutes_remaining",
    "blocker.escalation_minutes.T0",
    "blocker.escalation_minutes.T1",
    "blocker.escalation_minutes.T2",
    "blocker.escalation_minutes.T3",
    "blocker.escalation_minutes.T4",
    "dependency.edit_requires",
    "milestone.auto_confirm_allowed",
    "ai.control_profile",
    "retention.audit_years",
    "retention.event_years",
    "retention.evidence_years.default",
    "file.max_mb",
    "file.allowlist",
    "notifications.email.enabled",
    "notifications.teams.enabled",
    "notifications.slack.enabled",
    "readiness.failback_plan_exists",
    "readiness.critical_milestone_owner",
    "readiness.primary_system_owner",
    "readiness.primary_business_owner",
    "readiness.work_stream_lead",
    "readiness.task_owning_team",
    "readiness.dependency_graph_acyclic",
    "readiness.monitoring_task_present",
    "readiness.event_timezone_set",
    "readiness.needs_review_resolved",
    "readiness.coordinator_assigned",
    "readiness.application_in_scope",
    "readiness.rpo_target_or_na",
]

_TARGET_TYPE_COLUMNS = [
    "evidence_items",
    "comments",
    "overrides",
    "needs_review_items",
    "alerts",
    "notifications",
]

_NEW_TABLES_IN_ORDER = [
    "file_policy",
    "dr_event_participants",
    "outbox_events",
    "idempotency_keys",
    "reauth_grants",
    "sessions",
    "password_reset_tokens",
    "local_credentials",
]


def upgrade() -> None:
    sql = (_SPEC_DIR / "schema_v2_reconciliation.sql").read_text()
    lines = sql.splitlines()
    # Alembic already wraps this revision in its own transaction (D-247 migrations rule);
    # the file's own psql BEGIN;/COMMIT; would be redundant/invalid here.
    lines = [line for line in lines if line.strip() not in ("BEGIN;", "COMMIT;")]
    op.execute("\n".join(lines))


def downgrade() -> None:
    # 11/10. Seeded rows.
    op.execute("DELETE FROM file_policy WHERE singleton IS TRUE")
    keys = ", ".join(f"'{key}'" for key in _SEEDED_POLICY_KEYS)
    op.execute(f"DELETE FROM policy_definitions WHERE key IN ({keys})")

    # 9. Embeddings.
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_embedding_dim_ck")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS embedding_dim")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS embedding_model")

    # 8. RPO N/A.
    op.execute("ALTER TABLE dr_applications DROP CONSTRAINT IF EXISTS dr_applications_rpo_na_ck")
    op.execute("ALTER TABLE dr_applications DROP COLUMN IF EXISTS rpo_not_applicable")

    # 5. New tables (child-first order; FKs also allow CASCADE as a backstop).
    for table in _NEW_TABLES_IN_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    # 4. Task evidence / verification-note flags.
    op.execute("ALTER TABLE validations DROP COLUMN IF EXISTS verification_note")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_evidence_min_count_ck")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verification_note_required")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS evidence_min_count")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS evidence_required")

    # 3. Work Stream type.
    op.execute("DROP INDEX IF EXISTS ix_work_streams_event_type")
    op.execute("ALTER TABLE work_streams DROP COLUMN IF EXISTS stream_type")
    op.execute("DROP TYPE IF EXISTS work_stream_type")

    # 2. Canonical target_type enum -> back to TEXT.
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_entity_type_ck")
    op.execute("ALTER TABLE needs_review_items DROP CONSTRAINT IF EXISTS needs_review_items_target_type_ck")
    op.execute("ALTER TABLE evidence_items DROP CONSTRAINT IF EXISTS evidence_items_target_type_ck")
    op.execute("ALTER TABLE comments DROP CONSTRAINT IF EXISTS comments_target_type_ck")
    for table in _TARGET_TYPE_COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN target_type TYPE TEXT USING target_type::text")
    op.execute("DROP TYPE IF EXISTS target_type")

    # 1. dr_application_status enum -> v1 labels. FAILED_OVER has no v1 equivalent and maps to
    #    RECOVERED (documented, lossy; v1 never carried production data, D-247).
    op.execute("DROP INDEX IF EXISTS ix_dr_applications_event_status")
    op.execute("ALTER TYPE dr_application_status RENAME TO dr_application_status_v2")
    op.execute(
        "CREATE TYPE dr_application_status AS ENUM ("
        "'NOT_STARTED','IN_PROGRESS','RECOVERED','VALIDATING','FAILBACK_IN_PROGRESS','RESTORED'"
        ")"
    )
    op.execute("ALTER TABLE dr_applications ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE dr_applications ALTER COLUMN status TYPE dr_application_status USING ("
        "CASE status::text "
        "WHEN 'NOT_STARTED' THEN 'NOT_STARTED' "
        "WHEN 'RECOVERING' THEN 'IN_PROGRESS' "
        "WHEN 'TECHNICAL_VALIDATION' THEN 'VALIDATING' "
        "WHEN 'FAILED_OVER' THEN 'RECOVERED' "
        "WHEN 'FAILBACK_IN_PROGRESS' THEN 'FAILBACK_IN_PROGRESS' "
        "WHEN 'COMPLETED' THEN 'RESTORED' "
        "END"
        ")::dr_application_status"
    )
    op.execute("ALTER TABLE dr_applications ALTER COLUMN status SET DEFAULT 'NOT_STARTED'")
    op.execute("DROP TYPE dr_application_status_v2")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dr_applications_event_status "
        "ON dr_applications(dr_event_id, status) WHERE deleted_at IS NULL"
    )
