-- Prompt Wars: full PostgreSQL schema (Hybrid Monolith)
-- Apply: psql "$DATABASE_URL" -f database/init.sql

DROP VIEW IF EXISTS v_in_person_conversion CASCADE;

DROP TABLE IF EXISTS credit_ledger CASCADE;
DROP TABLE IF EXISTS participant_balances CASCADE;
DROP TABLE IF EXISTS registrations CASCADE;
DROP TABLE IF EXISTS virtual_challenge_submission_rows CASCADE;
DROP TABLE IF EXISTS in_person_challenge_submission_rows CASCADE;
DROP TABLE IF EXISTS challenges CASCADE;
DROP TABLE IF EXISTS submissions CASCADE;
DROP TABLE IF EXISTS rsvps CASCADE;
DROP TABLE IF EXISTS cities CASCADE;
DROP TABLE IF EXISTS participants CASCADE;
DROP TABLE IF EXISTS upload_archive CASCADE;
DROP TABLE IF EXISTS import_jobs CASCADE;
DROP TABLE IF EXISTS virtual_main_data_center_registrations CASCADE;
DROP TABLE IF EXISTS in_person_main_data_center_registrations CASCADE;
-- Legacy name (pre-split); safe no-op on fresh installs
DROP TABLE IF EXISTS main_data_center_registrations CASCADE;
DROP TABLE IF EXISTS events CASCADE;

CREATE TABLE events (
  id SERIAL PRIMARY KEY,
  parent_event_id INTEGER REFERENCES events (id) ON DELETE SET NULL,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('in_person', 'virtual')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_events_parent ON events (parent_event_id);
CREATE INDEX idx_events_kind ON events (kind);

CREATE TABLE import_jobs (
  id SERIAL PRIMARY KEY,
  module TEXT NOT NULL CHECK (module IN (
    'in_person',
    'virtual',
    'virtual_challenge_submissions',
    'in_person_challenge_submissions'
  )),
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  error_message TEXT,
  row_counts JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Forensic copy of every accepted upload, regardless of downstream success.
CREATE TABLE upload_archive (
  id BIGSERIAL PRIMARY KEY,
  module TEXT NOT NULL,
  source_route TEXT NOT NULL,
  original_name TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  size_bytes BIGINT NOT NULL,
  sha256 TEXT NOT NULL,
  mime_type TEXT,
  uploaded_by TEXT,
  client_ip TEXT,
  event_id INTEGER REFERENCES events (id) ON DELETE SET NULL,
  import_job_id INTEGER REFERENCES import_jobs (id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'received'
    CHECK (status IN ('received', 'parsed', 'success', 'failed')),
  error_message TEXT,
  rows_written INTEGER,
  uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_upload_archive_uploaded_at ON upload_archive (uploaded_at DESC);
CREATE INDEX idx_upload_archive_module ON upload_archive (module);
CREATE INDEX idx_upload_archive_sha256 ON upload_archive (sha256);

-- In-person Main Data Center: Vision export (CSV/XLSX). One row per email per in-person event.
CREATE TABLE in_person_main_data_center_registrations (
  id BIGSERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  email_normalized TEXT GENERATED ALWAYS AS (lower(trim(email))) STORED,
  form_timestamp TIMESTAMPTZ,
  utm_source TEXT,
  utm_medium TEXT,
  utm_campaign TEXT,
  utm_term TEXT,
  utm_content TEXT,
  org_name TEXT,
  org_state TEXT,
  org_city TEXT,
  class_stream TEXT,
  portfolio TEXT,
  domain TEXT,
  designation TEXT,
  designation_years_experience INTEGER,
  founded_info TEXT,
  degree TEXT,
  profile_name TEXT,
  full_name TEXT,
  mobile TEXT,
  whatsapp TEXT,
  country TEXT,
  state TEXT,
  city TEXT,
  dob DATE,
  gender TEXT,
  occupation TEXT,
  github_url TEXT,
  linkedin_url TEXT,
  attendance_city TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (event_id, email_normalized)
);

CREATE INDEX idx_ip_mdc_event ON in_person_main_data_center_registrations (event_id);
CREATE INDEX idx_ip_mdc_event_updated ON in_person_main_data_center_registrations (event_id, updated_at DESC);

-- Virtual Main Data Center: same shape as in-person export; scoped to virtual events only.
CREATE TABLE virtual_main_data_center_registrations (
  id BIGSERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  email_normalized TEXT GENERATED ALWAYS AS (lower(trim(email))) STORED,
  form_timestamp TIMESTAMPTZ,
  utm_source TEXT,
  utm_medium TEXT,
  utm_campaign TEXT,
  utm_term TEXT,
  utm_content TEXT,
  org_name TEXT,
  org_state TEXT,
  org_city TEXT,
  class_stream TEXT,
  portfolio TEXT,
  domain TEXT,
  designation TEXT,
  designation_years_experience INTEGER,
  founded_info TEXT,
  degree TEXT,
  profile_name TEXT,
  full_name TEXT,
  mobile TEXT,
  whatsapp TEXT,
  country TEXT,
  state TEXT,
  city TEXT,
  dob DATE,
  gender TEXT,
  occupation TEXT,
  github_url TEXT,
  linkedin_url TEXT,
  attendance_city TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (event_id, email_normalized)
);

CREATE INDEX idx_v_mdc_event ON virtual_main_data_center_registrations (event_id);
CREATE INDEX idx_v_mdc_event_updated ON virtual_main_data_center_registrations (event_id, updated_at DESC);

CREATE TABLE cities (
  id SERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  slug TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (event_id, name)
);

CREATE INDEX idx_cities_event ON cities (event_id);

CREATE TABLE participants (
  id SERIAL PRIMARY KEY,
  external_user_id TEXT UNIQUE,
  display_name TEXT,
  email TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_participants_external ON participants (external_user_id);

CREATE TABLE rsvps (
  id SERIAL PRIMARY KEY,
  participant_id INTEGER NOT NULL REFERENCES participants (id) ON DELETE CASCADE,
  city_id INTEGER NOT NULL REFERENCES cities (id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  rsvped_at TIMESTAMPTZ,
  import_job_id INTEGER REFERENCES import_jobs (id) ON DELETE SET NULL,
  UNIQUE (participant_id, city_id, event_id)
);

CREATE INDEX idx_rsvps_city ON rsvps (city_id);
CREATE INDEX idx_rsvps_event ON rsvps (event_id);

CREATE TABLE submissions (
  id SERIAL PRIMARY KEY,
  participant_id INTEGER NOT NULL REFERENCES participants (id) ON DELETE CASCADE,
  city_id INTEGER NOT NULL REFERENCES cities (id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  submitted_at TIMESTAMPTZ,
  import_job_id INTEGER REFERENCES import_jobs (id) ON DELETE SET NULL,
  UNIQUE (participant_id, city_id, event_id)
);

CREATE INDEX idx_submissions_city ON submissions (city_id);
CREATE INDEX idx_submissions_event ON submissions (event_id);

CREATE TABLE challenges (
  id SERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT,
  slug TEXT,
  /* When set, XLSX sheet suffix after "Submission " matches this (normalized), not title. */
  import_sheet_suffix TEXT,
  opens_at TIMESTAMPTZ,
  closes_at TIMESTAMPTZ,
  status TEXT NOT NULL CHECK (status IN ('draft', 'live', 'closed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_challenges_event ON challenges (event_id);
CREATE INDEX idx_challenges_live ON challenges (event_id) WHERE status = 'live';
CREATE UNIQUE INDEX uq_challenges_event_title_lower ON challenges (event_id, lower(title));
CREATE UNIQUE INDEX uq_challenges_event_import_sheet_suffix_lower
  ON challenges (event_id, lower(import_sheet_suffix))
  WHERE import_sheet_suffix IS NOT NULL AND btrim(import_sheet_suffix) <> '';

-- Touch trigger for challenges.updated_at
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

-- Virtual challenge workbook import: one row per team per challenge (distinct from in-person `submissions`).
CREATE TABLE virtual_challenge_submission_rows (
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

CREATE INDEX idx_vcsr_event ON virtual_challenge_submission_rows (event_id);
CREATE INDEX idx_vcsr_challenge ON virtual_challenge_submission_rows (challenge_id);
CREATE INDEX idx_vcsr_leader_email ON virtual_challenge_submission_rows (leader_email_normalized);
CREATE UNIQUE INDEX uq_vcsr_challenge_team ON virtual_challenge_submission_rows (challenge_id, team_name_normalized);
CREATE INDEX idx_vcsr_challenge_score_submitted ON virtual_challenge_submission_rows (
  challenge_id,
  total_score DESC NULLS LAST,
  export_created_at ASC NULLS LAST
);

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

-- In-person Action Center workbook: one row per team per PW session (city + date + optional label) per sheet kind.
CREATE TABLE in_person_challenge_submission_rows (
  id BIGSERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  import_job_id INTEGER REFERENCES import_jobs (id) ON DELETE SET NULL,
  in_person_mdc_registration_id BIGINT REFERENCES in_person_main_data_center_registrations (id) ON DELETE SET NULL,
  attendance_city TEXT NOT NULL,
  attendance_city_normalized TEXT GENERATED ALWAYS AS (lower(btrim(attendance_city))) STORED,
  prompt_war_on DATE NOT NULL DEFAULT DATE '1970-01-01',
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

CREATE INDEX idx_ipcsr_event ON in_person_challenge_submission_rows (event_id);
CREATE INDEX idx_ipcsr_leader_email ON in_person_challenge_submission_rows (leader_email_normalized);
CREATE UNIQUE INDEX uq_ipcsr_event_city_session_kind_team
  ON in_person_challenge_submission_rows (
    event_id,
    attendance_city_normalized,
    prompt_war_on,
    session_label_normalized,
    sheet_kind,
    team_name_normalized
  );

CREATE INDEX idx_ipcsr_event_kind_score ON in_person_challenge_submission_rows (
  event_id,
  sheet_kind,
  total_score DESC NULLS LAST,
  export_created_at ASC NULLS LAST,
  id ASC
);

CREATE INDEX idx_ipcsr_event_city_session_kind_score ON in_person_challenge_submission_rows (
  event_id,
  attendance_city_normalized,
  prompt_war_on,
  session_label_normalized,
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

CREATE TABLE registrations (
  id SERIAL PRIMARY KEY,
  participant_id INTEGER NOT NULL REFERENCES participants (id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (participant_id, event_id)
);

CREATE INDEX idx_registrations_event ON registrations (event_id);

CREATE TABLE credit_ledger (
  id BIGSERIAL PRIMARY KEY,
  participant_id INTEGER NOT NULL REFERENCES participants (id) ON DELETE CASCADE,
  challenge_id INTEGER REFERENCES challenges (id) ON DELETE SET NULL,
  delta NUMERIC(14, 4) NOT NULL,
  reason TEXT NOT NULL,
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  idempotency_key TEXT UNIQUE
);

CREATE INDEX idx_ledger_participant ON credit_ledger (participant_id);
CREATE INDEX idx_ledger_challenge ON credit_ledger (challenge_id);

CREATE TABLE participant_balances (
  participant_id INTEGER NOT NULL REFERENCES participants (id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  balance NUMERIC(14, 4) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (participant_id, event_id)
);

CREATE INDEX idx_balances_event_balance ON participant_balances (event_id, balance DESC);

CREATE OR REPLACE VIEW v_in_person_conversion AS
SELECT
  c.id AS city_id,
  c.event_id,
  c.name AS city_name,
  COALESCE(rc.cnt, 0)::BIGINT AS rsvp_count,
  COALESCE(sc.cnt, 0)::BIGINT AS submission_count,
  CASE
    WHEN COALESCE(rc.cnt, 0) = 0 THEN 0::NUMERIC
    ELSE ROUND((COALESCE(sc.cnt, 0)::NUMERIC / rc.cnt::NUMERIC), 6)
  END AS conversion_rate
FROM cities c
LEFT JOIN (
  SELECT city_id, COUNT(DISTINCT participant_id) AS cnt
  FROM rsvps
  GROUP BY city_id
) rc ON rc.city_id = c.id
LEFT JOIN (
  SELECT city_id, COUNT(DISTINCT participant_id) AS cnt
  FROM submissions
  GROUP BY city_id
) sc ON sc.city_id = c.id;

-- Demo seed (ids resolved by slug)
INSERT INTO events (slug, name, kind)
VALUES
  ('demo-in-person', 'Demo In-Person Tour', 'in_person'),
  ('demo-virtual', 'Demo Virtual Arena', 'virtual');

INSERT INTO cities (event_id, name, slug)
SELECT e.id, v.name, v.slug
FROM events e
CROSS JOIN (VALUES
  ('Austin', 'austin'),
  ('Dallas', 'dallas'),
  ('Houston', 'houston')
) AS v (name, slug)
WHERE e.slug = 'demo-in-person';

INSERT INTO challenges (event_id, title, opens_at, closes_at, status)
SELECT e.id, 'Live Prompt Battle', now() - interval '1 day', now() + interval '30 days', 'live'
FROM events e
WHERE e.slug = 'demo-virtual';
