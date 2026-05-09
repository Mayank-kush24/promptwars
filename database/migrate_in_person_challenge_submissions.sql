-- In-person Action Center workbook import: in_person_challenge_submission_rows + import_jobs module.
-- Idempotent; safe to re-run.

ALTER TABLE import_jobs DROP CONSTRAINT IF EXISTS import_jobs_module_check;
ALTER TABLE import_jobs ADD CONSTRAINT import_jobs_module_check
  CHECK (module IN (
    'in_person',
    'virtual',
    'virtual_challenge_submissions',
    'in_person_challenge_submissions'
  ));

CREATE TABLE IF NOT EXISTS in_person_challenge_submission_rows (
  id BIGSERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  import_job_id INTEGER REFERENCES import_jobs (id) ON DELETE SET NULL,
  in_person_mdc_registration_id BIGINT REFERENCES in_person_main_data_center_registrations (id) ON DELETE SET NULL,
  attendance_city TEXT NOT NULL,
  attendance_city_normalized TEXT GENERATED ALWAYS AS (lower(btrim(attendance_city))) STORED,
  prompt_war_on DATE,
  session_label TEXT NOT NULL DEFAULT '',
  session_label_normalized TEXT GENERATED ALWAYS AS (lower(btrim(session_label))) STORED,
  sheet_kind TEXT NOT NULL CHECK (sheet_kind IN ('warmup', 'main')),
  source_sheet_name TEXT NOT NULL,
  team_name TEXT NOT NULL,
  team_name_normalized TEXT GENERATED ALWAYS AS (lower(btrim(team_name))) STORED,
  leader_name TEXT,
  leader_email TEXT NOT NULL,
  leader_email_normalized TEXT GENERATED ALWAYS AS (lower(trim(leader_email))) STORED,
  leader_phone TEXT,
  team_size INTEGER,
  problem_statements TEXT,
  total_score NUMERIC(14, 4),
  deployed_link TEXT,
  deployed_changes_notes TEXT,
  github_repository_link TEXT,
  export_created_at TIMESTAMPTZ,
  export_created_by_name TEXT,
  export_created_by_email TEXT,
  export_updated_at TIMESTAMPTZ,
  export_updated_by_name TEXT,
  export_updated_by_email TEXT,
  imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ipcsr_event ON in_person_challenge_submission_rows (event_id);
CREATE INDEX IF NOT EXISTS idx_ipcsr_leader_email ON in_person_challenge_submission_rows (leader_email_normalized);

CREATE INDEX IF NOT EXISTS idx_ipcsr_event_kind_score ON in_person_challenge_submission_rows (
  event_id,
  sheet_kind,
  total_score DESC NULLS LAST,
  export_created_at ASC NULLS LAST,
  id ASC
);

CREATE OR REPLACE FUNCTION fn_ipcsr_touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS ipcsr_touch_updated_at ON in_person_challenge_submission_rows;
CREATE TRIGGER ipcsr_touch_updated_at
BEFORE UPDATE ON in_person_challenge_submission_rows
FOR EACH ROW
EXECUTE FUNCTION fn_ipcsr_touch_updated_at();

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'audit' AND p.proname = 'install_data_change_trigger'
  ) THEN
    PERFORM audit.install_data_change_trigger('public.in_person_challenge_submission_rows'::regclass);
  END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Prompt War session (date + optional label): widen natural key (idempotent).
-- ---------------------------------------------------------------------------
ALTER TABLE in_person_challenge_submission_rows
  ADD COLUMN IF NOT EXISTS prompt_war_on DATE;

ALTER TABLE in_person_challenge_submission_rows
  ADD COLUMN IF NOT EXISTS session_label TEXT NOT NULL DEFAULT '';

DO $ipcsr_sess$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'in_person_challenge_submission_rows'
      AND column_name = 'session_label_normalized'
  ) THEN
    ALTER TABLE in_person_challenge_submission_rows
      ADD COLUMN session_label_normalized TEXT
      GENERATED ALWAYS AS (lower(btrim(session_label))) STORED;
  END IF;
END
$ipcsr_sess$;

DROP INDEX IF EXISTS uq_ipcsr_event_city_kind_team;
DROP INDEX IF EXISTS idx_ipcsr_event_city_kind_score;
DROP INDEX IF EXISTS idx_ipcsr_event_city_session_kind_score;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ipcsr_event_city_session_kind_team
  ON in_person_challenge_submission_rows (
    event_id,
    attendance_city_normalized,
    prompt_war_on,
    session_label_normalized,
    sheet_kind,
    team_name_normalized
  );

CREATE INDEX IF NOT EXISTS idx_ipcsr_event_city_session_kind_score
  ON in_person_challenge_submission_rows (
    event_id,
    attendance_city_normalized,
    prompt_war_on,
    session_label_normalized,
    sheet_kind,
    total_score DESC NULLS LAST,
    export_created_at ASC NULLS LAST,
    id ASC
  );
