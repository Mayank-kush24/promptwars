-- Replace legacy sentinel DATE '1970-01-01' with NULL for "no Prompt War session date yet".
-- Makes in-person MDC / challenge submission rows align with SQL NULL semantics (no fake epoch date).
-- Idempotent; safe to re-run.
--
-- Order: DROP NOT NULL before any UPDATE that sets prompt_war_on to NULL (otherwise the UPDATE fails).

-- ---------------------------------------------------------------------------
-- 1) Nullable column, no default sentinel (must run before backfill UPDATEs)
-- ---------------------------------------------------------------------------
ALTER TABLE in_person_main_data_center_registrations
  ALTER COLUMN prompt_war_on DROP NOT NULL;

ALTER TABLE in_person_main_data_center_registrations
  ALTER COLUMN prompt_war_on DROP DEFAULT;

ALTER TABLE in_person_challenge_submission_rows
  ALTER COLUMN prompt_war_on DROP NOT NULL;

ALTER TABLE in_person_challenge_submission_rows
  ALTER COLUMN prompt_war_on DROP DEFAULT;

-- ---------------------------------------------------------------------------
-- 2) Backfill: treat epoch date as unknown session date
-- ---------------------------------------------------------------------------
UPDATE in_person_main_data_center_registrations
SET prompt_war_on = NULL
WHERE prompt_war_on = DATE '1970-01-01';

UPDATE in_person_challenge_submission_rows
SET prompt_war_on = NULL
WHERE prompt_war_on = DATE '1970-01-01';
