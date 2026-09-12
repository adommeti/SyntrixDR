-- DR Command Center — PostgreSQL bootstrap (runs once on an empty data volume)
-- Executed by the official image entrypoint as POSTGRES_USER against POSTGRES_DB
-- (psql 16, so \getenv is available).
--
-- Creates the extensions the schema needs (D-247: schema_v1.sql = Alembic 0001)
-- and a read-only role `drcc_readonly` for dashboards, tooling and the
-- read-only tooling. Password = $POSTGRES_RO_PASSWORD, else 'drcc_ro'
-- (matches .env.example DATABASE_URL_RO).
--
-- Re-running is safe: every statement is idempotent.

\set ON_ERROR_STOP on

-- pgvector: document_chunks.embedding vector(768) (D-233)
CREATE EXTENSION IF NOT EXISTS vector;
-- pgcrypto: gen_random_uuid() defaults throughout schema_v1.sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- pg_stat_statements: query telemetry (OPERATIONS.md observability); preloaded in compose
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Read-only role -------------------------------------------------------------
\set ro_password drcc_ro
\getenv ro_password POSTGRES_RO_PASSWORD
-- psql does not interpolate :'vars' inside $$ blocks; hand the value over via a GUC.
-- \gset swallows the result so the password never reaches the container log.
SELECT set_config('drcc.ro_password', :'ro_password', false) AS _ \gset

DO $$
DECLARE
  ro_password TEXT := coalesce(nullif(current_setting('drcc.ro_password', true), ''), 'drcc_ro');
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'drcc_readonly') THEN
    EXECUTE format(
      'CREATE ROLE drcc_readonly LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
      ro_password);
  ELSE
    EXECUTE format('ALTER ROLE drcc_readonly WITH LOGIN PASSWORD %L', ro_password);
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO drcc_readonly', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO drcc_readonly;

-- Objects that already exist (none on first boot; harmless on re-run).
GRANT SELECT ON ALL TABLES IN SCHEMA public TO drcc_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO drcc_readonly;

-- Every table/sequence the migration owner (current role = POSTGRES_USER, which
-- runs Alembic) creates later. Omitting FOR ROLE targets the current role.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO drcc_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO drcc_readonly;

-- Never allow the read-only role to create or write, even by accident.
REVOKE CREATE ON SCHEMA public FROM drcc_readonly;
ALTER ROLE drcc_readonly SET default_transaction_read_only = on;
ALTER ROLE drcc_readonly SET statement_timeout = '30s';
