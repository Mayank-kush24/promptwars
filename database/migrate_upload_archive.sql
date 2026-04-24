-- Optional one-time migration: add upload_archive table for forensic copies
-- of every accepted multipart upload (matches database/init.sql as of 2026-04).
--
-- Safe to re-run: every statement is idempotent.

-- 1) Create the table if it does not exist
CREATE TABLE IF NOT EXISTS upload_archive (
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

-- 2) Indexes
CREATE INDEX IF NOT EXISTS idx_upload_archive_uploaded_at ON upload_archive (uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_upload_archive_module ON upload_archive (module);
CREATE INDEX IF NOT EXISTS idx_upload_archive_sha256 ON upload_archive (sha256);

-- 3) Attach audit row trigger if database/audit.sql is installed.
--    Wrapped in DO block so it is a no-op when the audit schema is absent.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'audit' AND p.proname = 'install_data_change_trigger'
  ) THEN
    PERFORM audit.install_data_change_trigger('public.upload_archive'::regclass);
  END IF;
END
$$;
