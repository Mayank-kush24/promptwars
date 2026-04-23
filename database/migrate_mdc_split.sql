-- Optional one-time migration: split legacy `main_data_center_registrations`
-- into in-person vs virtual tables (matches database/init.sql as of 2026-04).
--
-- Review your actual index names (\d main_data_center_registrations) before
-- running the ALTER INDEX lines — auto-generated names differ by PG version.

-- 1) Rename legacy table → in-person
ALTER TABLE IF EXISTS main_data_center_registrations
  RENAME TO in_person_main_data_center_registrations;

-- 2) Rename legacy indexes (skip or adjust names if the rename above was a no-op)
-- ALTER INDEX idx_mdc_event RENAME TO idx_ip_mdc_event;
-- ALTER INDEX idx_mdc_event_updated RENAME TO idx_ip_mdc_event_updated;

-- 3) Create the virtual MDC table (empty until you import virtual exports)
CREATE TABLE IF NOT EXISTS virtual_main_data_center_registrations (
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

CREATE INDEX IF NOT EXISTS idx_v_mdc_event ON virtual_main_data_center_registrations (event_id);
CREATE INDEX IF NOT EXISTS idx_v_mdc_event_updated ON virtual_main_data_center_registrations (event_id, updated_at DESC);
