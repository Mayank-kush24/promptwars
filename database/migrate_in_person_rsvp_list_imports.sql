-- Manual RSVP email lists per PW session (invite sent vs accepted), for MDC overview
-- counts when Hawkeye is unavailable or as manual-first source of truth.
-- Idempotent; safe to re-run.

ALTER TABLE import_jobs DROP CONSTRAINT IF EXISTS import_jobs_module_check;
ALTER TABLE import_jobs ADD CONSTRAINT import_jobs_module_check
  CHECK (module IN (
    'in_person',
    'virtual',
    'virtual_challenge_submissions',
    'in_person_challenge_submissions',
    'in_person_rsvp_lists'
  ));

CREATE TABLE IF NOT EXISTS in_person_pw_session_rsvp_list_emails (
  id SERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  pw_session_id INTEGER NOT NULL REFERENCES in_person_pw_sessions (id) ON DELETE CASCADE,
  list_kind TEXT NOT NULL CHECK (list_kind IN ('invite_sent', 'accepted')),
  email_normalized TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  import_job_id INTEGER REFERENCES import_jobs (id) ON DELETE SET NULL,
  CONSTRAINT uq_ip_rsvp_list_email UNIQUE (pw_session_id, list_kind, email_normalized)
);

CREATE INDEX IF NOT EXISTS idx_ip_rsvp_list_emails_event_session_kind
  ON in_person_pw_session_rsvp_list_emails (event_id, pw_session_id, list_kind);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'audit' AND p.proname = 'install_data_change_trigger'
  ) THEN
    PERFORM audit.install_data_change_trigger('public.in_person_pw_session_rsvp_list_emails'::regclass);
  END IF;
END
$$;
