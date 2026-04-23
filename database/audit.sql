-- Prompt Wars: Master Audit Log schema
-- Apply AFTER database/init.sql (run_init_sql.py applies both in order)
--
-- Two coordinated layers:
--   * audit.audit_data_changes - row trigger written, atomic, transactional
--   * audit.audit_events       - app middleware written, async batched
--
-- An EVENT TRIGGER auto-attaches the row trigger to every new public table
-- so future modules / entities are covered without code changes.

CREATE SCHEMA IF NOT EXISTS audit;

-- ---------------------------------------------------------------------------
-- audit_data_changes (RANGE partitioned by occurred_at, monthly)
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS audit.audit_data_changes CASCADE;
CREATE TABLE audit.audit_data_changes (
  id              BIGSERIAL,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  txid            BIGINT NOT NULL DEFAULT txid_current(),
  db_user         TEXT NOT NULL DEFAULT current_user,
  schema_name     TEXT NOT NULL,
  table_name      TEXT NOT NULL,
  op              TEXT NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE','TRUNCATE','BULK')),
  record_pk       JSONB,
  old_row         JSONB,
  new_row         JSONB,
  changed_columns TEXT[],
  changes         JSONB,
  request_id      TEXT,
  principal       TEXT,
  principal_email TEXT,
  session_id      TEXT,
  client_ip       INET,
  source          TEXT,
  statement_tag   TEXT,
  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE INDEX idx_audit_data_changes_brin
  ON audit.audit_data_changes USING BRIN (occurred_at);
CREATE INDEX idx_audit_data_changes_table_time
  ON audit.audit_data_changes (table_name, occurred_at DESC);
CREATE INDEX idx_audit_data_changes_request
  ON audit.audit_data_changes (request_id);
CREATE INDEX idx_audit_data_changes_principal
  ON audit.audit_data_changes (principal, occurred_at DESC);
CREATE INDEX idx_audit_data_changes_changes_gin
  ON audit.audit_data_changes USING GIN (changes);

-- ---------------------------------------------------------------------------
-- audit_events (RANGE partitioned by occurred_at, monthly)
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS audit.audit_events CASCADE;
CREATE TABLE audit.audit_events (
  id              BIGSERIAL,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  request_id      TEXT,
  principal       TEXT,
  principal_email TEXT,
  session_id      TEXT,
  client_ip       INET,
  user_agent      TEXT,
  source          TEXT,
  action          TEXT NOT NULL,
  module          TEXT,
  entity          TEXT,
  record_pk       JSONB,
  http_method     TEXT,
  url             TEXT,
  endpoint        TEXT,
  status          INTEGER,
  latency_ms      NUMERIC(12, 3),
  sql_kind        TEXT,
  sql_statement   TEXT,
  rowcount        BIGINT,
  extra           JSONB,
  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE INDEX idx_audit_events_brin
  ON audit.audit_events USING BRIN (occurred_at);
CREATE INDEX idx_audit_events_request
  ON audit.audit_events (request_id);
CREATE INDEX idx_audit_events_action_time
  ON audit.audit_events (action, occurred_at DESC);
CREATE INDEX idx_audit_events_principal
  ON audit.audit_events (principal, occurred_at DESC);
CREATE INDEX idx_audit_events_endpoint
  ON audit.audit_events (endpoint, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- Partition management
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.ensure_partitions(months_ahead INTEGER DEFAULT 3)
RETURNS VOID
LANGUAGE plpgsql
AS $fn$
DECLARE
  parent     TEXT;
  start_d    DATE;
  end_d      DATE;
  part_name  TEXT;
  i          INTEGER;
BEGIN
  FOREACH parent IN ARRAY ARRAY['audit.audit_data_changes', 'audit.audit_events']
  LOOP
    FOR i IN -1..months_ahead LOOP
      start_d := date_trunc('month', (CURRENT_DATE + (i || ' months')::interval))::date;
      end_d   := (start_d + INTERVAL '1 month')::date;
      part_name := split_part(parent, '.', 2) || '_p' || to_char(start_d, 'YYYYMM');
      IF NOT EXISTS (
        SELECT 1 FROM pg_class WHERE relname = part_name AND relnamespace = 'audit'::regnamespace
      ) THEN
        EXECUTE format(
          'CREATE TABLE audit.%I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
          part_name, parent, start_d, end_d
        );
      END IF;
    END LOOP;
  END LOOP;
END;
$fn$;

-- Initial partitions (current month +/- buffer)
SELECT audit.ensure_partitions(3);

-- ---------------------------------------------------------------------------
-- Retention helper
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.drop_partitions_older_than(retention INTERVAL)
RETURNS INTEGER
LANGUAGE plpgsql
AS $fn$
DECLARE
  cutoff   TIMESTAMPTZ := now() - retention;
  rec      RECORD;
  dropped  INTEGER := 0;
BEGIN
  FOR rec IN
    SELECT c.relname AS part_name,
           pg_get_expr(c.relpartbound, c.oid) AS bound,
           p.relname AS parent_name
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class p ON p.oid = i.inhparent
    WHERE p.relnamespace = 'audit'::regnamespace
      AND p.relname IN ('audit_data_changes', 'audit_events')
  LOOP
    IF rec.bound ~ 'TO \(''([0-9-]+)' THEN
      IF (regexp_match(rec.bound, 'TO \(''([0-9-]+)'))[1]::date <= cutoff::date THEN
        EXECUTE format('DROP TABLE audit.%I', rec.part_name);
        dropped := dropped + 1;
      END IF;
    END IF;
  END LOOP;
  RETURN dropped;
END;
$fn$;

-- ---------------------------------------------------------------------------
-- Trigger function: row-level diff capture
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.fn_log_change()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fn$
DECLARE
  v_old_jsonb     JSONB;
  v_new_jsonb     JSONB;
  v_changes       JSONB := '{}'::jsonb;
  v_changed_cols  TEXT[] := ARRAY[]::TEXT[];
  v_pk            JSONB;
  v_pk_cols       TEXT[];
  v_request_id    TEXT;
  v_principal     TEXT;
  v_principal_em  TEXT;
  v_session_id    TEXT;
  v_client_ip     TEXT;
  v_source        TEXT;
  v_bulk_mode     TEXT;
  v_op            TEXT;
  k               TEXT;
  ov              JSONB;
  nv              JSONB;
BEGIN
  -- Self-skip: never audit anything in the audit schema
  IF TG_TABLE_SCHEMA = 'audit' THEN
    RETURN NULL;
  END IF;

  v_request_id   := NULLIF(current_setting('audit.request_id',   true), '');
  v_principal    := COALESCE(NULLIF(current_setting('audit.principal', true), ''), 'system:db');
  v_principal_em := NULLIF(current_setting('audit.principal_email', true), '');
  v_session_id   := NULLIF(current_setting('audit.session_id',   true), '');
  v_client_ip    := NULLIF(current_setting('audit.client_ip',    true), '');
  v_source       := COALESCE(NULLIF(current_setting('audit.source', true), ''), 'db');
  v_bulk_mode    := NULLIF(current_setting('audit.bulk_mode', true), '');

  -- Resolve PK columns once (best-effort)
  SELECT array_agg(a.attname::text ORDER BY x.ord)
    INTO v_pk_cols
  FROM pg_index i
  JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ord) ON true
  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = x.attnum
  WHERE i.indrelid = TG_RELID
    AND i.indisprimary;

  IF TG_OP = 'INSERT' THEN
    v_op := 'INSERT';
    v_new_jsonb := to_jsonb(NEW);
  ELSIF TG_OP = 'UPDATE' THEN
    v_op := 'UPDATE';
    v_old_jsonb := to_jsonb(OLD);
    v_new_jsonb := to_jsonb(NEW);
    FOR k IN SELECT jsonb_object_keys(v_new_jsonb) LOOP
      ov := v_old_jsonb -> k;
      nv := v_new_jsonb -> k;
      IF ov IS DISTINCT FROM nv THEN
        v_changed_cols := v_changed_cols || k;
        v_changes := v_changes || jsonb_build_object(k, jsonb_build_object('old', ov, 'new', nv));
      END IF;
    END LOOP;
    -- Skip writing audit row when no actual column changed (e.g. UPDATE with same values)
    IF cardinality(v_changed_cols) = 0 THEN
      RETURN NULL;
    END IF;
  ELSIF TG_OP = 'DELETE' THEN
    v_op := 'DELETE';
    v_old_jsonb := to_jsonb(OLD);
  ELSIF TG_OP = 'TRUNCATE' THEN
    v_op := 'TRUNCATE';
  ELSE
    RETURN NULL;
  END IF;

  -- Build PK JSONB from NEW (INSERT/UPDATE) or OLD (DELETE)
  IF v_pk_cols IS NOT NULL THEN
    IF TG_OP = 'DELETE' THEN
      SELECT jsonb_object_agg(c, v_old_jsonb -> c) INTO v_pk
      FROM unnest(v_pk_cols) AS c;
    ELSE
      SELECT jsonb_object_agg(c, v_new_jsonb -> c) INTO v_pk
      FROM unnest(v_pk_cols) AS c;
    END IF;
  END IF;

  INSERT INTO audit.audit_data_changes (
    occurred_at, schema_name, table_name, op, record_pk, old_row, new_row,
    changed_columns, changes, request_id, principal, principal_email,
    session_id, client_ip, source, statement_tag
  ) VALUES (
    clock_timestamp(),
    TG_TABLE_SCHEMA,
    TG_TABLE_NAME,
    v_op,
    v_pk,
    v_old_jsonb,
    v_new_jsonb,
    NULLIF(v_changed_cols, ARRAY[]::TEXT[]),
    NULLIF(v_changes, '{}'::jsonb),
    v_request_id,
    v_principal,
    v_principal_em,
    v_session_id,
    NULLIF(v_client_ip, '')::inet,
    v_source,
    LEFT(current_query(), 2048)
  );

  IF TG_OP = 'TRUNCATE' THEN
    RETURN NULL;
  ELSIF TG_OP = 'DELETE' THEN
    RETURN OLD;
  ELSE
    RETURN NEW;
  END IF;
END;
$fn$;

-- ---------------------------------------------------------------------------
-- Idempotent trigger installer for any user table
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.install_data_change_trigger(target regclass)
RETURNS VOID
LANGUAGE plpgsql
AS $fn$
DECLARE
  v_schema TEXT;
  v_name   TEXT;
BEGIN
  SELECT n.nspname, c.relname
    INTO v_schema, v_name
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.oid = target;

  IF v_schema IS NULL THEN
    RETURN;
  END IF;

  IF v_schema IN ('audit', 'pg_catalog', 'information_schema') THEN
    RETURN;
  END IF;

  -- Only attach to ordinary tables (skip partitions of partitioned tables to avoid double logging)
  IF NOT EXISTS (
    SELECT 1 FROM pg_class WHERE oid = target AND relkind IN ('r', 'p')
  ) THEN
    RETURN;
  END IF;

  EXECUTE format('DROP TRIGGER IF EXISTS pw_audit_row_trg ON %s', target::text);
  EXECUTE format(
    'CREATE TRIGGER pw_audit_row_trg AFTER INSERT OR UPDATE OR DELETE ON %s '
    'FOR EACH ROW EXECUTE FUNCTION audit.fn_log_change()',
    target::text
  );
  EXECUTE format(
    'DROP TRIGGER IF EXISTS pw_audit_truncate_trg ON %s', target::text
  );
  EXECUTE format(
    'CREATE TRIGGER pw_audit_truncate_trg AFTER TRUNCATE ON %s '
    'FOR EACH STATEMENT EXECUTE FUNCTION audit.fn_log_change()',
    target::text
  );
END;
$fn$;

-- ---------------------------------------------------------------------------
-- Event trigger: auto-attach row trigger to every newly created table
-- (fires once per CREATE TABLE; covers all future modules without code change)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.fn_on_create_table()
RETURNS event_trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
  obj record;
BEGIN
  FOR obj IN
    SELECT * FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
  LOOP
    BEGIN
      PERFORM audit.install_data_change_trigger(obj.objid::regclass);
    EXCEPTION WHEN OTHERS THEN
      -- never fail user DDL because of audit attachment
      NULL;
    END;
  END LOOP;
END;
$fn$;

DROP EVENT TRIGGER IF EXISTS pw_audit_auto_attach;
CREATE EVENT TRIGGER pw_audit_auto_attach
  ON ddl_command_end
  WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
  EXECUTE FUNCTION audit.fn_on_create_table();

-- ---------------------------------------------------------------------------
-- Bootstrap: attach row trigger to every existing user table in 'public'
-- ---------------------------------------------------------------------------
DO $bootstrap$
DECLARE
  rel record;
BEGIN
  FOR rel IN
    SELECT n.nspname AS schema_name, c.relname AS table_name, c.oid AS reloid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname NOT IN ('audit', 'pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_%'
  LOOP
    PERFORM audit.install_data_change_trigger(rel.reloid::regclass);
  END LOOP;
END;
$bootstrap$;
