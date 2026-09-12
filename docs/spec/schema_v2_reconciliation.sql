-- DR Command Center — schema_v2_reconciliation.sql
-- Target: PostgreSQL 16 + pgvector >= 0.5 (HNSW)
-- Alembic revision 0002 (upgrades a database created from schema_v1.sql / revision 0001)
-- Authority: FREEZE_ADDENDUM.md (D-2nn records, 2026-09-12) over FROZEN_DECISIONS.md.
--
-- Conventions
--   * Every block names the D-record it implements.
--   * Idempotent-safe where practical: IF NOT EXISTS / DROP ... IF EXISTS / DO-block guards,
--     so re-running against an already-upgraded database is a no-op.
--   * Runs in ONE transaction. No CREATE INDEX CONCURRENTLY, no ALTER TYPE ... ADD VALUE.
--   * Assumes a schema_v1 database with no rows that violate the new constraints
--     (fresh V1 databases; the v1 baseline never reached production data).
--   * BEGIN/COMMIT below are for psql execution. Alembic supplies its own transaction:
--     omit them when transcribing into revision 0002.
--
-- Block index
--   1. D-208  dr_application_status enum replacement
--   2. D-216  canonical target_type enum + 6 column conversions + per-table CHECKs
--             + audit_events.entity_type superset CHECK (stays TEXT)
--   3. D-227  work_stream_type enum + work_streams.stream_type
--   4. D-226  task evidence/verification-note requirement flags (+ validations.verification_note)
--   5. D-218(c) new tables: local_credentials, password_reset_tokens (D-235), sessions (D-239),
--             reauth_grants (D-235), idempotency_keys (D-215), outbox_events (D-242),
--             dr_event_participants (D-222), file_policy (D-225)
--   6. D-214  manager precedence — comment only
--   7. D-237  network_cut_at on start-failover — comment only
--   8. D-224  dr_applications.rpo_not_applicable
--   9. D-233  pgvector HNSW index + documents.embedding_model / embedding_dim
--  10. D-225 / D-224  policy_definitions seed
--  11. D-225  file_policy single-row seed

BEGIN;

-- =============================================================================
-- 1. D-208 — DR Application status enum
--    Supersedes schema_v1 dr_application_status
--    (NOT_STARTED, IN_PROGRESS, RECOVERED, VALIDATING, FAILBACK_IN_PROGRESS, RESTORED)
--    with NOT_STARTED -> RECOVERING -> TECHNICAL_VALIDATION -> FAILED_OVER
--         -> FAILBACK_IN_PROGRESS -> COMPLETED
--    Mapping: NOT_STARTED->NOT_STARTED, IN_PROGRESS->RECOVERING,
--             RECOVERED->TECHNICAL_VALIDATION, VALIDATING->TECHNICAL_VALIDATION,
--             FAILBACK_IN_PROGRESS->FAILBACK_IN_PROGRESS, RESTORED->COMPLETED.
--    Pattern: rename old type / create new type / ALTER COLUMN ... USING / drop old type.
--    Guard: skip if the enum already carries the new label RECOVERING.
-- =============================================================================
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
    WHERE t.typname = 'dr_application_status' AND e.enumlabel = 'RECOVERING'
  ) THEN
    ALTER TYPE dr_application_status RENAME TO dr_application_status_old;

    CREATE TYPE dr_application_status AS ENUM (
      'NOT_STARTED','RECOVERING','TECHNICAL_VALIDATION','FAILED_OVER',
      'FAILBACK_IN_PROGRESS','COMPLETED'
    );

    -- Partial index that references status: dropped here, recreated below.
    DROP INDEX IF EXISTS ix_dr_applications_event_status;

    ALTER TABLE dr_applications ALTER COLUMN status DROP DEFAULT;
    ALTER TABLE dr_applications
      ALTER COLUMN status TYPE dr_application_status
      USING (
        CASE status::text
          WHEN 'NOT_STARTED'          THEN 'NOT_STARTED'
          WHEN 'IN_PROGRESS'          THEN 'RECOVERING'
          WHEN 'RECOVERED'            THEN 'TECHNICAL_VALIDATION'
          WHEN 'VALIDATING'           THEN 'TECHNICAL_VALIDATION'
          WHEN 'FAILBACK_IN_PROGRESS' THEN 'FAILBACK_IN_PROGRESS'
          WHEN 'RESTORED'             THEN 'COMPLETED'
        END
      )::dr_application_status;
    ALTER TABLE dr_applications ALTER COLUMN status SET DEFAULT 'NOT_STARTED';

    DROP TYPE dr_application_status_old;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_dr_applications_event_status
  ON dr_applications(dr_event_id, status) WHERE deleted_at IS NULL;

-- =============================================================================
-- 2. D-216 — canonical polymorphic target enum
--    Replaces free-text target_type columns on
--    evidence_items, comments, overrides, needs_review_items, alerts, notifications.
--    validations.target_type keeps its own narrower validation_target_type enum (unchanged).
--    Existing indexes on the converted columns are rebuilt automatically by ALTER COLUMN TYPE.
--
--    Audit superset rule: audit_events.entity_type and external_references.entity_type
--    stay TEXT. The audit trail must also name admin/identity/config entities that are
--    deliberately NOT polymorphic targets (nobody comments on, attaches evidence to, or
--    overrides a USER or a POLICY_VALUE), e.g. role/policy/admin/security configuration
--    commands (D-228) and local-user/session events (D-235, D-239). audit_events.entity_type
--    is therefore constrained by CHECK to the SUPERSET: the 16 target_type labels plus the
--    admin/identity names. The CHECK list is the canonical audit entity catalog; extending it
--    requires an ADR/spec update (D-234) and a migration. external_references.entity_type is
--    left unconstrained TEXT (service-validated against the same superset).
-- =============================================================================
DO $$
BEGIN
  IF to_regtype('target_type') IS NULL THEN
    CREATE TYPE target_type AS ENUM (
      'DR_EVENT','DR_APPLICATION','APPLICATION','WORK_STREAM','TASK','TASK_DEPENDENCY',
      'MILESTONE','BLOCKER','ISSUE_FINDING','VALIDATION','IMPORT_JOB','PLAN',
      'PLAN_VERSION','REPORT','DOCUMENT','ALERT'
    );
  END IF;
END $$;

-- Convert each TEXT column to the enum (guard: only when the column is still TEXT).
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT * FROM (VALUES
      ('evidence_items',      'target_type'),
      ('comments',            'target_type'),
      ('overrides',           'target_type'),
      ('needs_review_items',  'target_type'),
      ('alerts',              'target_type'),
      ('notifications',       'target_type')
    ) AS v(tbl, col)
  LOOP
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = current_schema()
        AND table_name = r.tbl AND column_name = r.col AND udt_name = 'text'
    ) THEN
      EXECUTE format(
        'ALTER TABLE %I ALTER COLUMN %I TYPE target_type USING %I::target_type',
        r.tbl, r.col, r.col
      );
    END IF;
  END LOOP;
END $$;

-- Per-table subset CHECKs (D-216: "each table may accept only a subset").
ALTER TABLE comments DROP CONSTRAINT IF EXISTS comments_target_type_ck;
ALTER TABLE comments ADD CONSTRAINT comments_target_type_ck CHECK (
  target_type IN ('TASK','DR_APPLICATION','WORK_STREAM','BLOCKER','ISSUE_FINDING','DR_EVENT','MILESTONE')
);

ALTER TABLE evidence_items DROP CONSTRAINT IF EXISTS evidence_items_target_type_ck;
ALTER TABLE evidence_items ADD CONSTRAINT evidence_items_target_type_ck CHECK (
  target_type IN ('TASK','DR_APPLICATION','MILESTONE','BLOCKER','VALIDATION','ISSUE_FINDING')
);

ALTER TABLE needs_review_items DROP CONSTRAINT IF EXISTS needs_review_items_target_type_ck;
ALTER TABLE needs_review_items ADD CONSTRAINT needs_review_items_target_type_ck CHECK (
  target_type IN ('TASK','TASK_DEPENDENCY','DR_APPLICATION','IMPORT_JOB','DOCUMENT','MILESTONE')
);

-- overrides, alerts, notifications: any target_type value is permitted (no CHECK).

-- audit_events.entity_type (TEXT) — audit superset: 16 target_type labels + admin/identity names.
ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_entity_type_ck;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_entity_type_ck CHECK (
  entity_type IN (
    -- the 16 canonical polymorphic targets (must match enum target_type)
    'DR_EVENT','DR_APPLICATION','APPLICATION','WORK_STREAM','TASK','TASK_DEPENDENCY',
    'MILESTONE','BLOCKER','ISSUE_FINDING','VALIDATION','IMPORT_JOB','PLAN',
    'PLAN_VERSION','REPORT','DOCUMENT','ALERT',
    -- admin / identity / configuration entities (audit-only)
    'USER','TEAM','TEAM_MEMBERSHIP','ROLE_ASSIGNMENT','POLICY_VALUE','TIER','FILE_POLICY',
    'LOCAL_CREDENTIAL','SESSION','EXPORT_PACKAGE','SAVED_VIEW','LABEL','SKILL',
    'AI_CONFIG','EMAIL_CONFIG'
  )
);
-- external_references.entity_type stays TEXT, unconstrained (service-validated).

-- =============================================================================
-- 3. D-227 — Work Stream type
-- =============================================================================
DO $$
BEGIN
  IF to_regtype('work_stream_type') IS NULL THEN
    CREATE TYPE work_stream_type AS ENUM (
      'NETWORK','STORAGE','DATABASE','APPLICATIONS','MONITORING','VALIDATION','CUSTOM'
    );
  END IF;
END $$;

ALTER TABLE work_streams
  ADD COLUMN IF NOT EXISTS stream_type work_stream_type NOT NULL DEFAULT 'CUSTOM';

-- Supports readiness "≥1 MONITORING Work Stream Task" (D-224) and the close-time
-- outstanding-monitoring check (D-227).
CREATE INDEX IF NOT EXISTS ix_work_streams_event_type
  ON work_streams(dr_event_id, stream_type) WHERE deleted_at IS NULL;

-- =============================================================================
-- 4. D-226 — Task evidence / verification-note requirements
--    Normal completion = work done + >= evidence_min_count acceptable Evidence Items
--    + verification note + Owner/Lead validation. Enforcement is in the
--    submit-validation / validate services; the schema only carries the flags.
-- =============================================================================
ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS evidence_required BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS evidence_min_count SMALLINT NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS verification_note_required BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_evidence_min_count_ck;
ALTER TABLE tasks ADD CONSTRAINT tasks_evidence_min_count_ck CHECK (evidence_min_count >= 0);

-- Verification note storage decision:
--   The note is written at POST /tasks/{id}/submit-validation, which is exactly the
--   moment the persisted `validations` record is created (D-210). A Task may be
--   submitted, rejected and re-submitted, so the note is per-submission, not per-Task.
--   It therefore lives on `validations`, NOT on `tasks`. `validations.note` (v1) remains
--   the validator's decision note (approve/reject reason); the submitter's note gets its
--   own column so the two are never conflated in reports/audit.
ALTER TABLE validations
  ADD COLUMN IF NOT EXISTS verification_note TEXT;

-- =============================================================================
-- 5. D-218(c) — new tables
-- -----------------------------------------------------------------------------
-- 5a. D-235 — local account credentials (Argon2id, lockout, TOTP, forced reset).
--     schema_v1 deliberately kept credential hashes out of the domain schema; D-218(c)
--     brings them into revision 0002 as a dedicated auth table. Only users with
--     identity_type='LOCAL' may have a row (service-enforced; cross-table CHECK not possible).
-- =============================================================================
CREATE TABLE IF NOT EXISTS local_credentials (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
  password_hash TEXT NOT NULL,
  algorithm TEXT NOT NULL DEFAULT 'argon2id',
  failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
  locked_until TIMESTAMPTZ,                       -- 10 failures / 15 min -> +30 min
  password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  totp_secret_encrypted TEXT,                     -- encrypted at rest; never plaintext
  totp_enabled BOOLEAN NOT NULL DEFAULT FALSE,    -- required for Local GLOBAL_ADMIN
  must_reset BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT local_credentials_totp_ck CHECK (NOT totp_enabled OR totp_secret_encrypted IS NOT NULL)
);

-- 5b. D-235 — emailed single-use password reset tokens (30-min expiry). Only the hash is stored.
CREATE TABLE IF NOT EXISTS password_reset_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user_open
  ON password_reset_tokens(user_id, expires_at) WHERE used_at IS NULL;

-- 5c. D-239 — sessions.
--     Hot session state (cookie -> principal, CSRF token, last activity) lives in Redis.
--     This table is the durable audit / revocation record: it lets Admins list and revoke
--     sessions and lets replicas rebuild state after a Redis loss. `id` is the server-side
--     session identifier referenced from Redis; the browser cookie carries an opaque value.
--     8 h absolute / 30 min idle (D-235) are materialised as timestamps for reporting.
CREATE TABLE IF NOT EXISTS sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  identity_type identity_type NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  absolute_expires_at TIMESTAMPTZ NOT NULL,
  idle_expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  revoked_reason TEXT,
  ip TEXT,
  user_agent TEXT,
  CONSTRAINT sessions_expiry_ck CHECK (absolute_expires_at > created_at AND idle_expires_at >= created_at)
);
CREATE INDEX IF NOT EXISTS ix_sessions_user_active
  ON sessions(user_id, last_seen_at DESC) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_sessions_absolute_expires
  ON sessions(absolute_expires_at);

-- 5d. D-235 — privileged re-authentication grants (5-minute window).
--     method: how the user re-proved identity, e.g. 'PASSWORD', 'TOTP', 'ENTRA' (service enum).
CREATE TABLE IF NOT EXISTS reauth_grants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  method TEXT NOT NULL,
  CONSTRAINT reauth_grants_window_ck CHECK (expires_at > granted_at)
);
CREATE INDEX IF NOT EXISTS ix_reauth_grants_session
  ON reauth_grants(session_id, expires_at DESC);

-- 5e. D-215 — Idempotency-Key store (required on every command POST; retained 24 h).
--     A row is inserted when the command starts (response_* NULL = in flight) and completed
--     with the stored response. Same (key, user, route) with a different request_hash
--     -> 409/422 per API_CONTRACT. Expired rows are purged by the retention job.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key TEXT NOT NULL,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  route TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_status INTEGER CHECK (response_status IS NULL OR (response_status BETWEEN 100 AND 599)),
  response_body JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '24 hours'),
  CONSTRAINT idempotency_keys_unique UNIQUE (key, user_id, route)
);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_expires ON idempotency_keys(expires_at);

-- 5f. D-242 — transactional outbox (PostgreSQL tx -> outbox row -> worker -> Redis -> WS).
--     Written in the same transaction as the domain change; the worker publishes and stamps
--     published_at. attempts/last_error support retry and dead-letter reporting.
CREATE TABLE IF NOT EXISTS outbox_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  aggregate_type target_type NOT NULL,
  aggregate_id UUID NOT NULL,
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL,                      -- from the packages/contracts catalog (D-234)
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_events_unpublished
  ON outbox_events(occurred_at) WHERE published_at IS NULL;

-- 5g. D-222 — explicit DR Event participants (auto-enrolment sources + explicit adds).
--     Global Admins are implicit participants of every Event and are NOT materialised here.
--     Removal is soft (removed_at) so the enrolment history stays auditable; a user may hold
--     one active row per source.
CREATE TABLE IF NOT EXISTS dr_event_participants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  source TEXT NOT NULL CHECK (source IN (
    'EXPLICIT','ROLE','TASK_ASSIGNEE','BLOCKER_OWNER','APP_OWNER',
    'BUSINESS_OWNER','WORK_STREAM_LEAD','OWNING_TEAM'
  )),
  added_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  removed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dr_event_participants_active
  ON dr_event_participants(dr_event_id, user_id, source) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_dr_event_participants_user
  ON dr_event_participants(user_id) WHERE removed_at IS NULL;

-- 5h. D-225 — file policy (single row). Operational, effective upload policy used by the
--     ObjectStore/evidence services (size cap, extension + MIME allowlists, scan gate).
--     Admin edits go through the policy UI and must keep `file.max_mb` / `file.allowlist`
--     policy_definitions in step (service responsibility). `singleton` enforces one row.
CREATE TABLE IF NOT EXISTS file_policy (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  singleton BOOLEAN NOT NULL DEFAULT TRUE UNIQUE CHECK (singleton),
  max_bytes BIGINT NOT NULL DEFAULT 104857600 CHECK (max_bytes > 0),   -- 100 MiB
  allowed_extensions TEXT[] NOT NULL DEFAULT ARRAY[
    'pdf','png','jpg','jpeg','txt','log','csv','xlsx','docx','zip','json'
  ],
  allowed_mime_types TEXT[] NOT NULL DEFAULT ARRAY[
    'application/pdf',
    'image/png',
    'image/jpeg',
    'text/plain',
    'text/csv',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/zip',
    'application/x-zip-compressed',
    'application/json'
  ],
  scanning_required BOOLEAN NOT NULL DEFAULT TRUE,   -- D-245: INFECTED/unscanned never satisfies completion
  updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- 6. D-214 — Manager precedence (no schema change)
--    When a Team Manager's assignment with a stale expected_version collides with an
--    intervening Coordinator/Admin assignment on a Task owned by the Manager's Team, the
--    Manager's write is ACCEPTED (version increments). The resolution is recorded as:
--      * audit_events: both actions, with metadata.conflict_resolution = 'MANAGER_PRECEDENCE'
--      * overrides: one row with override_type = 'MANAGER_PRECEDENCE',
--        target_type = 'TASK', metadata = {superseded_version, superseded_assignee_user_id, ...}
--    No new table is introduced. All other stale writes -> 409 CONCURRENCY_CONFLICT.
-- =============================================================================

-- =============================================================================
-- 7. D-237 — start-failover network_cut_at (no schema change)
--    dr_events.network_cut_at (schema_v1) already stores the value. The service enforces
--    activated_at <= network_cut_at <= now(), defaults to server now(), and audits it.
-- =============================================================================

-- =============================================================================
-- 8. D-224 — RPO target or explicit N/A
--    readiness.rpo_target_or_na ("every DR App has RPO target or is explicitly RPO-N/A")
--    is a configurable readiness rule (HARD_STOP | WARNING | OFF) evaluated at activation.
--    It is deliberately NOT a database CHECK: a CHECK would (a) make the WARNING/OFF settings
--    unreachable, (b) forbid adding an Application to a PLANNED Event before its RPO is known,
--    and (c) fail this migration on any v1 row with rpo_target_minutes IS NULL.
--    The schema enforces only the true invariant: a DR App cannot be both RPO-N/A and carry
--    an RPO target.
-- =============================================================================
ALTER TABLE dr_applications
  ADD COLUMN IF NOT EXISTS rpo_not_applicable BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE dr_applications DROP CONSTRAINT IF EXISTS dr_applications_rpo_na_ck;
ALTER TABLE dr_applications ADD CONSTRAINT dr_applications_rpo_na_ck CHECK (
  NOT (rpo_not_applicable AND rpo_target_minutes IS NOT NULL)
);

-- =============================================================================
-- 9. D-233 — embeddings: nomic-embed-text-v1.5, 768 dims, frozen.
--    document_chunks.embedding is already vector(768) in schema_v1. The ANN index can now be
--    created. HNSW build inside a transaction is permitted (no CONCURRENTLY).
--    documents.embedding_model / embedding_dim track what each document was embedded with so
--    a model/dimension change triggers re-embedding and vectors are never mixed.
-- =============================================================================
CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding
  ON document_chunks USING hnsw (embedding vector_cosine_ops);

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT 'nomic-embed-text-v1.5',
  ADD COLUMN IF NOT EXISTS embedding_dim SMALLINT NOT NULL DEFAULT 768;

ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_embedding_dim_ck;
ALTER TABLE documents ADD CONSTRAINT documents_embedding_dim_ck CHECK (embedding_dim > 0);

-- =============================================================================
-- 10. D-225 / D-224 — policy_definitions seed
--     value_type vocabulary: integer | integer_nullable | boolean | enum | string_array | object
--     readiness.* default_value is the severity: HARD_STOP | WARNING | OFF.
--     readiness.dependency_graph_acyclic is fixed at HARD_STOP and NOT configurable
--     (service must reject policy_values for it).
--     Per-tier blocker escalation is one key per tier so Event-scoped policy_values can
--     override a single tier.
-- =============================================================================
INSERT INTO policy_definitions (key, name, description, value_type, default_value) VALUES
  -- SLA
  ('sla.warning_percent',            'SLA warning threshold (%)',
     'Percent of the SLA/RTO window consumed at which the SLA warning overlay fires.',
     'integer', '85'),
  ('sla.warning_minutes_remaining',  'SLA warning (minutes remaining)',
     'Alternative absolute threshold; null disables.',
     'integer_nullable', 'null'),
  -- Blocker escalation per tier (minutes an OPEN blocker may sit before escalation)
  ('blocker.escalation_minutes.T0',  'Blocker escalation minutes — Tier 0', NULL, 'integer', '0'),
  ('blocker.escalation_minutes.T1',  'Blocker escalation minutes — Tier 1', NULL, 'integer', '15'),
  ('blocker.escalation_minutes.T2',  'Blocker escalation minutes — Tier 2', NULL, 'integer', '30'),
  ('blocker.escalation_minutes.T3',  'Blocker escalation minutes — Tier 3', NULL, 'integer', '60'),
  ('blocker.escalation_minutes.T4',  'Blocker escalation minutes — Tier 4', NULL, 'integer', '120'),
  -- Dependencies / milestones / AI
  ('dependency.edit_requires',       'Dependency edit authority',
     'SCOPED_ROLE (default) or COORDINATOR_ONLY.',
     'enum', '"SCOPED_ROLE"'),
  ('milestone.auto_confirm_allowed', 'Allow AUTOMATIC milestone confirmation', NULL, 'boolean', 'false'),
  ('ai.control_profile',             'Default AI control profile',
     'CONSERVATIVE | BALANCED | FAST_EXECUTION (D-228).',
     'enum', '"CONSERVATIVE"'),
  -- Retention (D-258)
  ('retention.audit_years',          'Audit retention (years)', NULL, 'integer', '7'),
  ('retention.event_years',          'DR Event / Task retention (years)', NULL, 'integer', '3'),
  ('retention.evidence_years.default','Evidence retention default (years)',
     'Classification-specific values may override.',
     'integer', '3'),
  -- Files (effective values mirrored in file_policy)
  ('file.max_mb',                    'Maximum upload size (MB)', NULL, 'integer', '100'),
  ('file.allowlist',                 'Allowed upload extensions', NULL, 'string_array',
     '["pdf","png","jpg","jpeg","txt","log","csv","xlsx","docx","zip","json"]'),
  -- Notification channels (D-256)
  ('notifications.email.enabled',    'Email notifications enabled', NULL, 'boolean', 'true'),
  ('notifications.teams.enabled',    'Teams notifications enabled', 'Coming Soon; disabled in V1.', 'boolean', 'false'),
  ('notifications.slack.enabled',    'Slack notifications enabled', 'Coming Soon; disabled in V1.', 'boolean', 'false'),
  -- Readiness catalog (D-224): HARD_STOP | WARNING | OFF
  ('readiness.failback_plan_exists',       'Readiness: failback plan exists for every failback_required App', NULL, 'enum', '"HARD_STOP"'),
  ('readiness.critical_milestone_owner',   'Readiness: every critical manual Milestone has an owner',       NULL, 'enum', '"HARD_STOP"'),
  ('readiness.primary_system_owner',       'Readiness: every in-scope App has a Primary System Owner',      NULL, 'enum', '"HARD_STOP"'),
  ('readiness.primary_business_owner',     'Readiness: Primary Business Owner present',                     NULL, 'enum', '"WARNING"'),
  ('readiness.work_stream_lead',           'Readiness: every Work Stream has a Lead',                       NULL, 'enum', '"WARNING"'),
  ('readiness.task_owning_team',           'Readiness: every Task has an Owning Team',                      NULL, 'enum', '"HARD_STOP"'),
  ('readiness.dependency_graph_acyclic',   'Readiness: dependency graph is acyclic',
     'Fixed at HARD_STOP; not configurable.',
     'enum', '"HARD_STOP"'),
  ('readiness.monitoring_task_present',    'Readiness: at least one MONITORING Work Stream Task',           NULL, 'enum', '"WARNING"'),
  ('readiness.event_timezone_set',         'Readiness: Event timezone set',                                 NULL, 'enum', '"HARD_STOP"'),
  ('readiness.needs_review_resolved',      'Readiness: no unresolved import/AI Needs Review items',         NULL, 'enum', '"WARNING"'),
  ('readiness.coordinator_assigned',       'Readiness: DR Coordinator assigned',                            NULL, 'enum', '"HARD_STOP"'),
  ('readiness.application_in_scope',       'Readiness: at least one DR Application in scope',               NULL, 'enum', '"HARD_STOP"'),
  ('readiness.rpo_target_or_na',           'Readiness: every DR App has an RPO target or is explicitly RPO-N/A', NULL, 'enum', '"HARD_STOP"')
ON CONFLICT (key) DO NOTHING;

-- =============================================================================
-- 11. D-225 — file_policy single-row seed (column defaults carry the D-225 values)
-- =============================================================================
INSERT INTO file_policy (singleton) VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

COMMIT;
