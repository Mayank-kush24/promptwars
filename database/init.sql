-- Prompt Wars: full PostgreSQL schema (Hybrid Monolith)
-- Apply: psql "$DATABASE_URL" -f database/init.sql

DROP VIEW IF EXISTS v_in_person_conversion CASCADE;

DROP TABLE IF EXISTS credit_ledger CASCADE;
DROP TABLE IF EXISTS participant_balances CASCADE;
DROP TABLE IF EXISTS registrations CASCADE;
DROP TABLE IF EXISTS challenges CASCADE;
DROP TABLE IF EXISTS submissions CASCADE;
DROP TABLE IF EXISTS rsvps CASCADE;
DROP TABLE IF EXISTS cities CASCADE;
DROP TABLE IF EXISTS participants CASCADE;
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
  module TEXT NOT NULL CHECK (module IN ('in_person', 'virtual')),
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  error_message TEXT,
  row_counts JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
  opens_at TIMESTAMPTZ,
  closes_at TIMESTAMPTZ,
  status TEXT NOT NULL CHECK (status IN ('draft', 'live', 'closed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_challenges_event ON challenges (event_id);
CREATE INDEX idx_challenges_live ON challenges (event_id) WHERE status = 'live';
CREATE UNIQUE INDEX uq_challenges_event_title_lower ON challenges (event_id, lower(title));

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
