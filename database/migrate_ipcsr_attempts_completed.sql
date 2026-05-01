-- In-person Action Center: attempts per team (optional column on workbook import).
-- Apply: psql "$DATABASE_URL" -f database/migrate_ipcsr_attempts_completed.sql

ALTER TABLE in_person_challenge_submission_rows
  ADD COLUMN IF NOT EXISTS attempts_completed INTEGER;

COMMENT ON COLUMN in_person_challenge_submission_rows.attempts_completed IS
  'Optional; from Action Center "Attempts completed" column. Used for submission analytics.';
