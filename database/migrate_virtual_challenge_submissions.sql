-- Virtual challenge submission workbook import: new table + challenges.import_sheet_suffix
-- + import_jobs module value. Idempotent; safe to re-run.
--
-- 0) If `challenges` predates migrate_virtual_challenges.sql, add columns the app INSERT expects
--    (description, slug, updated_at) plus title uniqueness + touch trigger.

ALTER TABLE challenges
  ADD COLUMN IF NOT EXISTS description TEXT,
  ADD COLUMN IF NOT EXISTS slug TEXT,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS uq_challenges_event_title_lower
  ON challenges (event_id, lower(title));

CREATE OR REPLACE FUNCTION fn_challenges_touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS challenges_touch_updated_at ON challenges;
CREATE TRIGGER challenges_touch_updated_at
BEFORE UPDATE ON challenges
FOR EACH ROW
EXECUTE FUNCTION fn_challenges_touch_updated_at();

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'audit' AND p.proname = 'install_data_change_trigger'
  ) THEN
    PERFORM audit.install_data_change_trigger('public.challenges'::regclass);
  END IF;
END
$$;

-- 1) challenges.import_sheet_suffix
ALTER TABLE challenges
  ADD COLUMN IF NOT EXISTS import_sheet_suffix TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_challenges_event_import_sheet_suffix_lower
  ON challenges (event_id, lower(import_sheet_suffix))
  WHERE import_sheet_suffix IS NOT NULL AND btrim(import_sheet_suffix) <> '';

-- 2) import_jobs: allow virtual_challenge_submissions module label
ALTER TABLE import_jobs DROP CONSTRAINT IF EXISTS import_jobs_module_check;
ALTER TABLE import_jobs ADD CONSTRAINT import_jobs_module_check
  CHECK (module IN (
    'in_person',
    'virtual',
    'virtual_challenge_submissions',
    'in_person_challenge_submissions'
  ));

-- 3) Main data table
CREATE TABLE IF NOT EXISTS virtual_challenge_submission_rows (
  id BIGSERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  challenge_id INTEGER NOT NULL REFERENCES challenges (id) ON DELETE CASCADE,
  import_job_id INTEGER REFERENCES import_jobs (id) ON DELETE SET NULL,
  virtual_mdc_registration_id BIGINT REFERENCES virtual_main_data_center_registrations (id) ON DELETE SET NULL,
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
  linkedin_post TEXT,
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

CREATE INDEX IF NOT EXISTS idx_vcsr_event ON virtual_challenge_submission_rows (event_id);
CREATE INDEX IF NOT EXISTS idx_vcsr_challenge ON virtual_challenge_submission_rows (challenge_id);
CREATE INDEX IF NOT EXISTS idx_vcsr_leader_email ON virtual_challenge_submission_rows (leader_email_normalized);
CREATE UNIQUE INDEX IF NOT EXISTS uq_vcsr_challenge_team
  ON virtual_challenge_submission_rows (challenge_id, team_name_normalized);

CREATE OR REPLACE FUNCTION fn_vcsr_touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS vcsr_touch_updated_at ON virtual_challenge_submission_rows;
CREATE TRIGGER vcsr_touch_updated_at
BEFORE UPDATE ON virtual_challenge_submission_rows
FOR EACH ROW
EXECUTE FUNCTION fn_vcsr_touch_updated_at();

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'audit' AND p.proname = 'install_data_change_trigger'
  ) THEN
    PERFORM audit.install_data_change_trigger('public.virtual_challenge_submission_rows'::regclass);
  END IF;
END
$$;

-- Leaderboard sort: score desc, submitted asc
CREATE INDEX IF NOT EXISTS idx_vcsr_challenge_score_submitted ON virtual_challenge_submission_rows (
  challenge_id,
  total_score DESC NULLS LAST,
  export_created_at ASC NULLS LAST
);
