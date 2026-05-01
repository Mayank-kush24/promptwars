-- PW Sessions: explicit in_person_pw_sessions + FKs from MDC, Action Center rows, Hawkeye snapshots.
-- Idempotent; safe to re-run.
--
-- Note: GENERATED columns must use immutable expressions only.
--   - date::text is not immutable (depends on DateStyle), so scope_key uses EXTRACT + lpad for ISO date.
--   - display_name uses to_char(..., 'Mon') (STABLE); set via trigger instead of GENERATED.

-- ---------------------------------------------------------------------------
-- 1) Session table (city = attendance slug, lower(trim) from backfill)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS in_person_pw_sessions (
  id SERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  city TEXT NOT NULL,
  prompt_war_on DATE NOT NULL,
  session_label TEXT NOT NULL DEFAULT '',
  scope_key TEXT GENERATED ALWAYS AS (
    city
    || '|'
    || lpad(extract(year FROM prompt_war_on)::int::text, 4, '0')
    || '-' || lpad(extract(month FROM prompt_war_on)::int::text, 2, '0')
    || '-' || lpad(extract(day FROM prompt_war_on)::int::text, 2, '0')
    || '|'
    || session_label
  ) STORED,
  display_name TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_in_person_pw_sessions_event_city_date_label UNIQUE (
    event_id, city, prompt_war_on, session_label
  )
);

CREATE OR REPLACE FUNCTION fn_in_person_pw_sessions_set_display_name()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.display_name :=
    initcap(NEW.city)
    || ' · '
    || to_char(NEW.prompt_war_on::timestamp, 'DD Mon YYYY')
    || CASE
         WHEN btrim(COALESCE(NEW.session_label, '')) <> '' THEN ' · ' || NEW.session_label
         ELSE ''
       END;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tr_in_person_pw_sessions_display ON in_person_pw_sessions;
CREATE TRIGGER tr_in_person_pw_sessions_display
  BEFORE INSERT OR UPDATE OF city, prompt_war_on, session_label
  ON in_person_pw_sessions
  FOR EACH ROW
  EXECUTE PROCEDURE fn_in_person_pw_sessions_set_display_name();

CREATE INDEX IF NOT EXISTS idx_in_person_pw_sessions_event_id
  ON in_person_pw_sessions (event_id);

CREATE INDEX IF NOT EXISTS idx_in_person_pw_sessions_scope_key
  ON in_person_pw_sessions (scope_key);

-- ---------------------------------------------------------------------------
-- 2) Backfill sessions from MDC (exclude legacy sentinel date)
-- ---------------------------------------------------------------------------
INSERT INTO in_person_pw_sessions (event_id, city, prompt_war_on, session_label)
SELECT DISTINCT
  r.event_id,
  lower(trim(both FROM COALESCE(r.attendance_city, ''))),
  r.prompt_war_on,
  COALESCE(r.session_label, '')
FROM in_person_main_data_center_registrations r
WHERE r.prompt_war_on IS NOT NULL
  AND r.prompt_war_on <> DATE '1970-01-01'
  AND trim(COALESCE(r.attendance_city, '')) <> ''
ON CONFLICT (event_id, city, prompt_war_on, session_label) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3) FK + backfill: in_person_main_data_center_registrations
-- ---------------------------------------------------------------------------
ALTER TABLE in_person_main_data_center_registrations
  ADD COLUMN IF NOT EXISTS pw_session_id INTEGER REFERENCES in_person_pw_sessions (id);

UPDATE in_person_main_data_center_registrations r
SET pw_session_id = s.id
FROM in_person_pw_sessions s
WHERE r.pw_session_id IS NULL
  AND r.event_id = s.event_id
  AND lower(trim(both FROM COALESCE(r.attendance_city, ''))) = s.city
  AND r.prompt_war_on = s.prompt_war_on
  AND COALESCE(r.session_label, '') = s.session_label;

-- ---------------------------------------------------------------------------
-- 4) FK + backfill: in_person_challenge_submission_rows
-- ---------------------------------------------------------------------------
ALTER TABLE in_person_challenge_submission_rows
  ADD COLUMN IF NOT EXISTS pw_session_id INTEGER REFERENCES in_person_pw_sessions (id);

UPDATE in_person_challenge_submission_rows r
SET pw_session_id = s.id
FROM in_person_pw_sessions s
WHERE r.pw_session_id IS NULL
  AND r.event_id = s.event_id
  AND lower(trim(both FROM COALESCE(r.attendance_city, ''))) = s.city
  AND r.prompt_war_on = s.prompt_war_on
  AND COALESCE(r.session_label, '') = s.session_label;

-- ---------------------------------------------------------------------------
-- 5) Hawkeye snapshots + external mappings
-- ---------------------------------------------------------------------------
ALTER TABLE hawkeye_rsvp_snapshots
  ADD COLUMN IF NOT EXISTS pw_session_id INTEGER REFERENCES in_person_pw_sessions (id);

COMMENT ON COLUMN hawkeye_rsvp_snapshots.pw_session_id IS
  'Rows with pw_session_id NULL are legacy/unresolved — should be cleaned up or reassigned before legacy support is removed.';

ALTER TABLE event_external_mappings
  ADD COLUMN IF NOT EXISTS pw_session_id INTEGER REFERENCES in_person_pw_sessions (id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_event_external_mappings_pw_session_id
  ON event_external_mappings (pw_session_id)
  WHERE pw_session_id IS NOT NULL;
