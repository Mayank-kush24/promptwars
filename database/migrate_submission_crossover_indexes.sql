-- Optional: speed cohort scans for cross-track submission analytics (see services/submission_analytics.py).
-- Apply: psql "$DATABASE_URL" -f database/migrate_submission_crossover_indexes.sql

CREATE INDEX IF NOT EXISTS idx_vcsr_event_leader_email
  ON virtual_challenge_submission_rows (event_id, leader_email_normalized);

CREATE INDEX IF NOT EXISTS idx_ipcsr_event_leader_email
  ON in_person_challenge_submission_rows (event_id, leader_email_normalized);

CREATE INDEX IF NOT EXISTS idx_ipcsr_event_city_leader_email
  ON in_person_challenge_submission_rows (
    event_id,
    attendance_city_normalized,
    leader_email_normalized
  );
