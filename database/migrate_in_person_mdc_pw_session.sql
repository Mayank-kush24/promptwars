-- In-person MDC: Prompt War session per registration (city + date + optional label).
-- Idempotent; safe to re-run.

-- 1) Session columns (legacy = sentinel date + empty label → "City · (legacy)" in UI)
ALTER TABLE in_person_main_data_center_registrations
  ADD COLUMN IF NOT EXISTS prompt_war_on DATE NOT NULL DEFAULT DATE '1970-01-01',
  ADD COLUMN IF NOT EXISTS session_label TEXT NOT NULL DEFAULT '';

-- 2) Drop old uniqueness (one row per email per event)
ALTER TABLE in_person_main_data_center_registrations
  DROP CONSTRAINT IF EXISTS in_person_main_data_center_registrations_event_id_email_normalized_key;

-- 3) Rebuild generated column + composite unique (drops dependent objects first)
ALTER TABLE in_person_main_data_center_registrations
  DROP CONSTRAINT IF EXISTS uq_ip_mdc_event_email_pw_session;

ALTER TABLE in_person_main_data_center_registrations
  DROP COLUMN IF EXISTS session_label_normalized;

ALTER TABLE in_person_main_data_center_registrations
  ADD COLUMN session_label_normalized TEXT GENERATED ALWAYS AS (lower(btrim(session_label))) STORED;

ALTER TABLE in_person_main_data_center_registrations
  ADD CONSTRAINT uq_ip_mdc_event_email_pw_session UNIQUE (
    event_id,
    email_normalized,
    prompt_war_on,
    session_label_normalized
  );

CREATE INDEX IF NOT EXISTS idx_ip_mdc_event_pw ON in_person_main_data_center_registrations (event_id, prompt_war_on);
CREATE INDEX IF NOT EXISTS idx_ip_mdc_event_city ON in_person_main_data_center_registrations (event_id, attendance_city);
