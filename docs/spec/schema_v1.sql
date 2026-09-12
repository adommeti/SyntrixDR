-- DR Command Center V1 PostgreSQL baseline
-- Target: PostgreSQL 16+
-- FROZEN V1 SCHEMA BASELINE — 2026-09-11
-- Reconciled 2026-09-12: see schema_v2_reconciliation.sql (Alembic 0002) for D-2nn changes.
-- Product questions are frozen. Local-auth credential hashes remain intentionally outside this domain schema and must use a dedicated auth implementation.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TYPE identity_type AS ENUM ('ENTRA','LOCAL');
CREATE TYPE role_scope_type AS ENUM ('GLOBAL','DR_EVENT','WORK_STREAM','APPLICATION');
CREATE TYPE dr_event_type AS ENUM ('PLANNED_DR','REAL_INCIDENT');
CREATE TYPE dr_event_status AS ENUM (
  'PLANNED','ACTIVE','FAILOVER_IN_PROGRESS','FAILED_OVER',
  'FAILBACK_IN_PROGRESS','CLOSED','CANCELLED'
);
CREATE TYPE dr_application_status AS ENUM (
  'NOT_STARTED','IN_PROGRESS','RECOVERED','VALIDATING',
  'FAILBACK_IN_PROGRESS','RESTORED'
);
CREATE TYPE plan_type AS ENUM ('FAILOVER','FAILBACK','GENERAL');
CREATE TYPE plan_source_type AS ENUM ('MANUAL','EXCEL_IMPORT','CLONE','AI_ASSISTED');
CREATE TYPE plan_version_type AS ENUM ('DRAFT','BASELINE','EXECUTION','FINAL');
CREATE TYPE task_phase AS ENUM ('PRE_DR','FAILOVER','VALIDATION','FAILBACK','POST_DR');

CREATE TYPE task_status AS ENUM (
  'NOT_STARTED','IN_PROGRESS','BLOCKED','READY_FOR_VALIDATION',
  'COMPLETED','CANCELLED'
);

CREATE TYPE dependency_type AS ENUM ('FINISH_TO_START');
CREATE TYPE dependency_strength AS ENUM ('HARD','ADVISORY');
CREATE TYPE blocker_status AS ENUM ('OPEN','ASSIGNED','IN_PROGRESS','RESOLVED','VERIFIED','CLOSED');
CREATE TYPE validation_target_type AS ENUM ('TASK','DR_APPLICATION','MILESTONE');
CREATE TYPE validation_status AS ENUM ('PENDING','IN_REVIEW','APPROVED','REJECTED','REMEDIATION');
CREATE TYPE milestone_status AS ENUM (
  'NOT_STARTED','IN_PROGRESS','AT_RISK','READY_FOR_CONFIRMATION','ACHIEVED','MISSED'
);
CREATE TYPE milestone_confirmation_mode AS ENUM ('MANUAL','AUTOMATIC');
CREATE TYPE evidence_type AS ENUM ('TEXT','FILE','SCREENSHOT','LOG','EXTERNAL_LINK');
CREATE TYPE saved_view_visibility AS ENUM ('PRIVATE','SHARED');
CREATE TYPE ai_control_profile AS ENUM ('CONSERVATIVE','BALANCED','FAST_EXECUTION');
CREATE TYPE report_status AS ENUM ('DRAFT','PUBLISHED');
CREATE TYPE review_source AS ENUM ('AI','EXCEL_IMPORT','SEMANTIC_INFERENCE');
CREATE TYPE review_status AS ENUM ('OPEN','IN_REVIEW','RESOLVED','DISMISSED');
CREATE TYPE health_band AS ENUM ('GREEN','LIGHT_ORANGE','DARK_ORANGE','RED');

CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entra_object_id TEXT UNIQUE,
  identity_type identity_type NOT NULL,
  display_name TEXT NOT NULL,
  email TEXT NOT NULL,
  job_title TEXT,
  manager_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  avatar_uri TEXT,
  mascot_uri TEXT,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT users_identity_ck CHECK (
    (identity_type='ENTRA' AND entra_object_id IS NOT NULL)
    OR identity_type='LOCAL'
  )
);
CREATE UNIQUE INDEX ux_users_email_active ON users(lower(email)) WHERE deleted_at IS NULL;
CREATE INDEX ix_users_manager ON users(manager_user_id) WHERE deleted_at IS NULL;

CREATE TABLE teams (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  description TEXT,
  manager_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  mascot_uri TEXT,
  external_system TEXT,
  external_id TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_teams_name_active ON teams(lower(name)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX ux_teams_external_ref ON teams(external_system,external_id)
  WHERE external_system IS NOT NULL AND external_id IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE team_memberships (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id UUID NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  membership_role TEXT,
  effective_from TIMESTAMPTZ,
  effective_to TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT team_membership_dates_ck CHECK (
    effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from
  )
);
CREATE UNIQUE INDEX ux_team_memberships_active ON team_memberships(team_id,user_id)
  WHERE deleted_at IS NULL;

CREATE TABLE skills (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  description TEXT,
  category TEXT,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_skills_name ON skills(lower(name));

CREATE TABLE user_skills (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  skill_id UUID NOT NULL REFERENCES skills(id) ON DELETE RESTRICT,
  proficiency NUMERIC(5,2),
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id,skill_id)
);

CREATE TABLE role_assignments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  role_key TEXT NOT NULL,
  scope_type role_scope_type NOT NULL,
  scope_id UUID,
  granted_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  CONSTRAINT role_scope_ck CHECK (
    (scope_type='GLOBAL' AND scope_id IS NULL)
    OR (scope_type <> 'GLOBAL' AND scope_id IS NOT NULL)
  )
);
CREATE INDEX ix_role_assignments_active ON role_assignments(user_id,role_key,scope_type,scope_id)
  WHERE revoked_at IS NULL;

CREATE TABLE tiers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code TEXT NOT NULL UNIQUE,
  rank INTEGER NOT NULL UNIQUE CHECK(rank >= 0),
  default_sla_minutes INTEGER NOT NULL CHECK(default_sla_minutes > 0),
  default_health_weight NUMERIC(10,2) NOT NULL CHECK(default_health_weight >= 0),
  description TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE applications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  description TEXT,
  tier_id UUID NOT NULL REFERENCES tiers(id) ON DELETE RESTRICT,
  external_system TEXT,
  external_id TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_applications_name_active ON applications(lower(name)) WHERE deleted_at IS NULL;


CREATE TYPE application_owner_type AS ENUM ('SYSTEM_APPLICATION','BUSINESS');

CREATE TABLE application_owners (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id UUID NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,
  owner_type application_owner_type NOT NULL,
  owner_order SMALLINT NOT NULL CHECK (owner_order BETWEEN 1 AND 3),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT application_owner_slot_unique UNIQUE(application_id, owner_type, owner_order)
);
CREATE INDEX ix_application_owners_user ON application_owners(user_id) WHERE deleted_at IS NULL;

CREATE TABLE plans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  description TEXT,
  plan_type plan_type NOT NULL,
  application_id UUID REFERENCES applications(id) ON DELETE RESTRICT,
  source_type plan_source_type NOT NULL DEFAULT 'MANUAL',
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE dr_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  name TEXT NOT NULL,
  event_type dr_event_type NOT NULL,
  description TEXT,
  status dr_event_status NOT NULL DEFAULT 'PLANNED',
  coordinator_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  source_location TEXT,
  target_location TEXT,
  planned_start_at TIMESTAMPTZ,
  event_timezone TEXT NOT NULL DEFAULT 'UTC',
  network_cut_at TIMESTAMPTZ,
  failback_started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ,
  cancel_reason TEXT,
  ai_control_profile ai_control_profile NOT NULL DEFAULT 'CONSERVATIVE',
  baseline_plan_version_id UUID,
  health_score NUMERIC(7,4),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT dr_events_cancel_ck CHECK(status <> 'CANCELLED' OR cancel_reason IS NOT NULL)
);
CREATE INDEX ix_dr_events_status ON dr_events(status) WHERE deleted_at IS NULL;

CREATE TABLE plan_versions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id UUID REFERENCES plans(id) ON DELETE RESTRICT,
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  version_number INTEGER NOT NULL CHECK(version_number > 0),
  version_type plan_version_type NOT NULL,
  notes TEXT,
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT plan_version_parent_ck CHECK(plan_id IS NOT NULL OR dr_event_id IS NOT NULL)
);
CREATE UNIQUE INDEX ux_plan_versions_plan_version ON plan_versions(plan_id,version_number)
  WHERE plan_id IS NOT NULL;
ALTER TABLE dr_events ADD CONSTRAINT fk_event_baseline
  FOREIGN KEY (baseline_plan_version_id) REFERENCES plan_versions(id) ON DELETE RESTRICT;

CREATE TABLE dr_applications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  application_id UUID NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,
  effective_tier_id UUID NOT NULL REFERENCES tiers(id) ON DELETE RESTRICT,
  effective_sla_minutes INTEGER NOT NULL CHECK(effective_sla_minutes > 0),
  rto_target_minutes INTEGER NOT NULL CHECK(rto_target_minutes > 0),
  rto_actual_minutes INTEGER CHECK(rto_actual_minutes IS NULL OR rto_actual_minutes >= 0),
  rto_passed BOOLEAN,
  rpo_target_minutes INTEGER CHECK(rpo_target_minutes IS NULL OR rpo_target_minutes >= 0),
  rpo_reference_at TIMESTAMPTZ,
  rpo_recovered_data_at TIMESTAMPTZ,
  rpo_actual_loss_minutes INTEGER CHECK(rpo_actual_loss_minutes IS NULL OR rpo_actual_loss_minutes >= 0),
  rpo_passed BOOLEAN,
  failback_required BOOLEAN NOT NULL DEFAULT TRUE,
  status dr_application_status NOT NULL DEFAULT 'NOT_STARTED',
  sla_start_at TIMESTAMPTZ,
  technical_validated_at TIMESTAMPTZ,
  sla_breached_at TIMESTAMPTZ,
  target_recovery_at TIMESTAMPTZ,
  forecast_recovery_at TIMESTAMPTZ,
  failback_target_at TIMESTAMPTZ,
  failback_forecast_at TIMESTAMPTZ,
  business_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
  business_confirmation_note TEXT,
  health_band health_band,
  health_score NUMERIC(7,4),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  UNIQUE(dr_event_id,application_id)
);
CREATE INDEX ix_dr_applications_event_status ON dr_applications(dr_event_id,status)
  WHERE deleted_at IS NULL;

CREATE TABLE work_streams (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  name TEXT NOT NULL,
  description TEXT,
  lead_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  owning_team_id UUID REFERENCES teams(id) ON DELETE SET NULL,
  sequence_order INTEGER,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_work_stream_event_name ON work_streams(dr_event_id,lower(name))
  WHERE deleted_at IS NULL;

CREATE TABLE tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  dr_application_id UUID REFERENCES dr_applications(id) ON DELETE RESTRICT,
  work_stream_id UUID REFERENCES work_streams(id) ON DELETE RESTRICT,
  parent_task_id UUID REFERENCES tasks(id) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  description TEXT,
  phase task_phase NOT NULL,
  status task_status NOT NULL DEFAULT 'NOT_STARTED',
  owning_team_id UUID NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
  current_assignee_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  default_owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  backup_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  escalation_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  expected_duration_minutes INTEGER CHECK(expected_duration_minutes IS NULL OR expected_duration_minutes >= 0),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  needs_specific_validation BOOLEAN NOT NULL DEFAULT FALSE,
  added_during_execution BOOLEAN NOT NULL DEFAULT FALSE,
  source_import_id UUID,
  source_import_row TEXT,
  sort_order INTEGER,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT task_context_ck CHECK(dr_application_id IS NOT NULL OR work_stream_id IS NOT NULL)
);
CREATE INDEX ix_tasks_event_status ON tasks(dr_event_id,status) WHERE deleted_at IS NULL;
CREATE INDEX ix_tasks_application ON tasks(dr_application_id,status) WHERE deleted_at IS NULL;
CREATE INDEX ix_tasks_work_stream ON tasks(work_stream_id,status) WHERE deleted_at IS NULL;
CREATE INDEX ix_tasks_assignee ON tasks(current_assignee_user_id,status) WHERE deleted_at IS NULL;
CREATE INDEX ix_tasks_team ON tasks(owning_team_id,status) WHERE deleted_at IS NULL;

CREATE TABLE task_dependencies (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  predecessor_task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  successor_task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  dependency_type dependency_type NOT NULL DEFAULT 'FINISH_TO_START',
  strength dependency_strength NOT NULL DEFAULT 'HARD',
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT no_self_dependency CHECK(predecessor_task_id <> successor_task_id)
);
CREATE UNIQUE INDEX ux_task_dependency ON task_dependencies(predecessor_task_id,successor_task_id,dependency_type)
  WHERE deleted_at IS NULL;

CREATE TABLE milestones (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  work_stream_id UUID REFERENCES work_streams(id) ON DELETE RESTRICT,
  dr_application_id UUID REFERENCES dr_applications(id) ON DELETE RESTRICT,
  name TEXT NOT NULL,
  description TEXT,
  status milestone_status NOT NULL DEFAULT 'NOT_STARTED',
  owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  confirmation_mode milestone_confirmation_mode NOT NULL DEFAULT 'MANUAL',
  ready_for_confirmation_at TIMESTAMPTZ,
  achieved_at TIMESTAMPTZ,
  target_at TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT milestone_context_ck CHECK(work_stream_id IS NOT NULL OR dr_application_id IS NOT NULL)
);

CREATE TABLE milestone_tasks (
  milestone_id UUID NOT NULL REFERENCES milestones(id) ON DELETE RESTRICT,
  task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  is_required BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(milestone_id,task_id)
);

CREATE TABLE milestone_dependencies (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  milestone_id UUID NOT NULL REFERENCES milestones(id) ON DELETE RESTRICT,
  successor_task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  strength dependency_strength NOT NULL DEFAULT 'HARD',
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_milestone_dependency ON milestone_dependencies(milestone_id,successor_task_id)
  WHERE deleted_at IS NULL;

CREATE TABLE blockers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  reason TEXT NOT NULL CHECK(length(btrim(reason)) > 0),
  category TEXT,
  status blocker_status NOT NULL DEFAULT 'OPEN',
  blocker_team_id UUID REFERENCES teams(id) ON DELETE SET NULL,
  blocker_owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  blocked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ,
  verified_at TIMESTAMPTZ,
  closed_at TIMESTAMPTZ,
  resolution_note TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
CREATE INDEX ix_blockers_task_active ON blockers(task_id,status)
  WHERE deleted_at IS NULL AND status <> 'CLOSED';
CREATE INDEX ix_blockers_team_active ON blockers(blocker_team_id,status)
  WHERE deleted_at IS NULL AND status <> 'CLOSED';


CREATE TYPE issue_finding_status AS ENUM ('OPEN','IN_REVIEW','RESOLVED','DISMISSED');
CREATE TYPE issue_finding_type AS ENUM ('ISSUE','FINDING','RISK','DEVIATION','LESSON');

CREATE TABLE issue_findings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  dr_application_id UUID REFERENCES dr_applications(id) ON DELETE RESTRICT,
  work_stream_id UUID REFERENCES work_streams(id) ON DELETE RESTRICT,
  task_id UUID REFERENCES tasks(id) ON DELETE RESTRICT,
  item_type issue_finding_type NOT NULL,
  status issue_finding_status NOT NULL DEFAULT 'OPEN',
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  priority TEXT,
  owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  resolution_note TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ
);
CREATE INDEX ix_issue_findings_event_status ON issue_findings(dr_event_id, status) WHERE deleted_at IS NULL;

CREATE TABLE validations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  target_type validation_target_type NOT NULL,
  target_id UUID NOT NULL,
  status validation_status NOT NULL DEFAULT 'PENDING',
  validator_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  submitted_at TIMESTAMPTZ,
  reviewed_at TIMESTAMPTZ,
  approved_at TIMESTAMPTZ,
  note TEXT,
  business_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
  business_confirmation_note TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
CREATE INDEX ix_validations_target ON validations(target_type,target_id) WHERE deleted_at IS NULL;

CREATE TABLE evidence_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  target_type TEXT NOT NULL,
  target_id UUID NOT NULL,
  evidence_type evidence_type NOT NULL,
  text_content TEXT,
  external_url TEXT,
  blob_uri TEXT,
  file_name TEXT,
  mime_type TEXT,
  size_bytes BIGINT CHECK(size_bytes IS NULL OR size_bytes >= 0),
  checksum TEXT,
  malware_scan_status TEXT,
  malware_scanned_at TIMESTAMPTZ,
  uploaded_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT evidence_payload_ck CHECK(
    (evidence_type IN ('TEXT','LOG') AND text_content IS NOT NULL)
    OR (evidence_type='EXTERNAL_LINK' AND external_url IS NOT NULL)
    OR (evidence_type IN ('FILE','SCREENSHOT') AND blob_uri IS NOT NULL)
  )
);
CREATE INDEX ix_evidence_target ON evidence_items(target_type,target_id) WHERE deleted_at IS NULL;

CREATE TABLE comments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  target_type TEXT NOT NULL,
  target_id UUID NOT NULL,
  author_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  body TEXT NOT NULL CHECK(length(btrim(body)) > 0),
  parent_comment_id UUID REFERENCES comments(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ
);
CREATE INDEX ix_comments_target ON comments(target_type,target_id,created_at)
  WHERE deleted_at IS NULL;

CREATE TABLE mentions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  comment_id UUID NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
  mentioned_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(comment_id,mentioned_user_id)
);

CREATE TABLE alerts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  alert_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  target_type TEXT,
  target_id UUID,
  acknowledged_at TIMESTAMPTZ,
  acknowledged_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  alert_id UUID REFERENCES alerts(id) ON DELETE RESTRICT,
  notification_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  target_type TEXT,
  target_id UUID,
  read_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_notifications_user_unread ON notifications(user_id,created_at DESC)
  WHERE read_at IS NULL;

CREATE TABLE alert_snoozes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  alert_id UUID NOT NULL REFERENCES alerts(id) ON DELETE RESTRICT,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  snoozed_until TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(alert_id,user_id)
);

CREATE TABLE labels (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  name TEXT NOT NULL,
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE task_labels (
  task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
  label_id UUID NOT NULL REFERENCES labels(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(task_id,label_id)
);

CREATE TABLE saved_views (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  name TEXT NOT NULL,
  visibility saved_view_visibility NOT NULL DEFAULT 'PRIVATE',
  definition JSONB NOT NULL DEFAULT '{}'::jsonb,
  published_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE policy_definitions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT,
  value_type TEXT NOT NULL,
  default_value JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE policy_values (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  policy_definition_id UUID NOT NULL REFERENCES policy_definitions(id) ON DELETE RESTRICT,
  scope_type role_scope_type NOT NULL,
  scope_id UUID,
  value JSONB NOT NULL,
  changed_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  superseded_at TIMESTAMPTZ,
  CONSTRAINT policy_scope_ck CHECK(
    (scope_type='GLOBAL' AND scope_id IS NULL)
    OR (scope_type <> 'GLOBAL' AND scope_id IS NOT NULL)
  )
);

CREATE TABLE overrides (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  target_type TEXT NOT NULL,
  target_id UUID NOT NULL,
  override_type TEXT NOT NULL,
  policy_key TEXT,
  reason TEXT NOT NULL CHECK(length(btrim(reason)) > 0),
  performed_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  actor_type TEXT NOT NULL DEFAULT 'USER',
  entity_type TEXT NOT NULL,
  entity_id UUID NOT NULL,
  action TEXT NOT NULL,
  before_data JSONB,
  after_data JSONB,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_event_time ON audit_events(dr_event_id,occurred_at DESC);
CREATE INDEX ix_audit_entity ON audit_events(entity_type,entity_id,occurred_at DESC);

CREATE TABLE needs_review_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  target_type TEXT NOT NULL,
  target_id UUID NOT NULL,
  reason TEXT NOT NULL,
  source review_source NOT NULL,
  confidence NUMERIC(7,6) CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  status review_status NOT NULL DEFAULT 'OPEN',
  assigned_scope_type TEXT,
  assigned_scope_id UUID,
  resolved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  resolved_at TIMESTAMPTZ,
  resolution_note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE reports (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL UNIQUE REFERENCES dr_events(id) ON DELETE RESTRICT,
  status report_status NOT NULL DEFAULT 'DRAFT',
  current_version_id UUID,
  published_at TIMESTAMPTZ,
  published_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE report_versions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  report_id UUID NOT NULL REFERENCES reports(id) ON DELETE RESTRICT,
  version_number INTEGER NOT NULL CHECK(version_number > 0),
  narrative TEXT NOT NULL,
  facts_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  generated_by_ai BOOLEAN NOT NULL DEFAULT FALSE,
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(report_id,version_number)
);
ALTER TABLE reports ADD CONSTRAINT fk_report_current_version
  FOREIGN KEY(current_version_id) REFERENCES report_versions(id) ON DELETE RESTRICT;

CREATE TABLE export_packages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  tenant_id TEXT NOT NULL,
  package_type TEXT NOT NULL,
  encryption_metadata JSONB NOT NULL,
  integrity_hash TEXT NOT NULL,
  storage_uri TEXT NOT NULL,
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE external_references (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type TEXT NOT NULL,
  entity_id UUID NOT NULL,
  system_name TEXT NOT NULL,
  external_id TEXT NOT NULL,
  external_url TEXT,
  last_synced_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(entity_type,entity_id,system_name),
  UNIQUE(system_name,external_id)
);

CREATE TABLE documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID REFERENCES dr_events(id) ON DELETE RESTRICT,
  application_id UUID REFERENCES applications(id) ON DELETE RESTRICT,
  task_id UUID REFERENCES tasks(id) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  blob_uri TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  file_name TEXT,
  checksum TEXT,
  malware_scan_status TEXT,
  malware_scanned_at TIMESTAMPTZ,
  uploaded_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  indexed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ
);

CREATE TABLE document_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
  text_content TEXT NOT NULL,
  embedding vector(768),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  search_vector TSVECTOR GENERATED ALWAYS AS
    (to_tsvector('english',coalesce(text_content,''))) STORED,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(document_id,chunk_index)
);
CREATE INDEX ix_document_chunks_fts ON document_chunks USING GIN(search_vector);
-- Add fixed-dimension pgvector ANN index only after embedding model/dimension is approved.

CREATE TABLE import_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  source_file_uri TEXT NOT NULL,
  status TEXT NOT NULL,
  mapping_data JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_data JSONB,
  created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ
);

CREATE TABLE plan_version_tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_version_id UUID NOT NULL REFERENCES plan_versions(id) ON DELETE RESTRICT,
  source_task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
  snapshot_data JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(plan_version_id,source_task_id)
);

CREATE TABLE plan_version_task_dependencies (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_version_id UUID NOT NULL REFERENCES plan_versions(id) ON DELETE RESTRICT,
  source_dependency_id UUID REFERENCES task_dependencies(id) ON DELETE SET NULL,
  snapshot_data JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plan_version_milestones (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_version_id UUID NOT NULL REFERENCES plan_versions(id) ON DELETE RESTRICT,
  source_milestone_id UUID REFERENCES milestones(id) ON DELETE SET NULL,
  snapshot_data JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dr_health_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dr_event_id UUID NOT NULL REFERENCES dr_events(id) ON DELETE RESTRICT,
  overall_score NUMERIC(7,4) NOT NULL,
  band health_band NOT NULL,
  calculation_version TEXT NOT NULL,
  inputs JSONB NOT NULL,
  calculated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_health_snapshot_event_time ON dr_health_snapshots(dr_event_id,calculated_at DESC);

-- Frozen V1 tier defaults. Admin-configurable at runtime; Event overrides are audited.
INSERT INTO tiers(code, rank, default_sla_minutes, default_health_weight) VALUES
  ('TIER_0',0,30,100),
  ('TIER_1',1,60,75),
  ('TIER_2',2,120,50),
  ('TIER_3',3,240,30),
  ('TIER_4',4,1440,20)
ON CONFLICT (code) DO UPDATE SET
  rank=EXCLUDED.rank,
  default_sla_minutes=EXCLUDED.default_sla_minutes,
  default_health_weight=EXCLUDED.default_health_weight;

-- All five tiers (TIER_0..TIER_4) are seeded above with the frozen health weights
-- 100 / 75 / 50 / 30 / 20. Tier 3 = 30 per FROZEN_DECISIONS §10 (D-218). No tier is
-- left unseeded and no Tier 0 cap applies to the overall dial.


-- Frozen business rules enforced in application transition services:
-- * Derived READY = NOT_STARTED plus all HARD prerequisites/manual gates satisfied.
-- * BLOCKED Task must have an active Blocker record.
-- * Task protected completion requires required evidence + verification/validation note.
-- * Application Owner validates application work; Work Stream Lead validates shared work.
-- * Common regional network-cut timestamp initializes RTO/SLA clocks.
-- * RTO stops only at technical application validation; a recorded breach is never erased.
-- * RPO uses target + pre-DR reference + recovered-data timestamp to calculate pass/fail.
-- * Manager assignment scope is own Team; Coordinator/Admin may reassign cross-Team.
-- * Owning Team remains stable when Current Assignee changes Team.
-- * AI actions call the same application commands and cannot bypass RBAC/guards/reauth/audit.
-- * Event CLOSED/CANCELLED are terminal for operational execution in V1.
