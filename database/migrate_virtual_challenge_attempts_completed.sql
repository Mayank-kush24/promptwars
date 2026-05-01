-- Optional: add per-team attempt counts for virtual arena submissions.
-- Apply: psql "$DATABASE_URL" -f database/migrate_virtual_challenge_attempts_completed.sql

ALTER TABLE virtual_challenge_submission_rows
  ADD COLUMN IF NOT EXISTS attempts_completed INTEGER;

COMMENT ON COLUMN virtual_challenge_submission_rows.attempts_completed IS
  'Number of attempts completed for this team/challenge (from attempt sheet import or workbook column).';
