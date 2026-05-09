-- In-person MDC: one row per (event_id, email), idempotent import (create + update).
-- Fixes:
--   1) Legacy unique kept the old name after RENAME (main_data_center_registrations → in_person_…),
--      so migrate_in_person_mdc_pw_session.sql never dropped it.
--   2) Re-imports with a different (prompt_war_on, session) must UPDATE the same row, not insert another.
--
-- Safe to re-run: drops target constraints if present, repoints FKs, removes duplicate MDC rows, then adds uq_ip_mdc_event_email.

-- ---------------------------------------------------------------------------
-- 1) Drop stale / superseded uniqueness
-- ---------------------------------------------------------------------------
ALTER TABLE in_person_main_data_center_registrations
  DROP CONSTRAINT IF EXISTS main_data_center_registrations_event_id_email_normalized_key;

ALTER TABLE in_person_main_data_center_registrations
  DROP CONSTRAINT IF EXISTS in_person_main_data_center_registrations_event_id_email_normalized_key;

ALTER TABLE in_person_main_data_center_registrations
  DROP CONSTRAINT IF EXISTS uq_ip_mdc_event_email_pw_session;

ALTER TABLE in_person_main_data_center_registrations
  DROP CONSTRAINT IF EXISTS uq_ip_mdc_event_email;

-- ---------------------------------------------------------------------------
-- 2) Repoint challenge rows that reference duplicate MDC ids (before delete)
-- ---------------------------------------------------------------------------
WITH dup AS (
  SELECT id,
         FIRST_VALUE(id) OVER (
           PARTITION BY event_id, email_normalized
           ORDER BY updated_at DESC NULLS LAST, id DESC
         ) AS keeper_id
  FROM in_person_main_data_center_registrations
)
UPDATE in_person_challenge_submission_rows c
SET in_person_mdc_registration_id = d.keeper_id
FROM dup d
WHERE c.in_person_mdc_registration_id = d.id
  AND d.id <> d.keeper_id;

-- ---------------------------------------------------------------------------
-- 3) Keep newest row per (event_id, email_normalized)
-- ---------------------------------------------------------------------------
DELETE FROM in_person_main_data_center_registrations
WHERE id IN (
  SELECT id
  FROM (
    SELECT id,
           ROW_NUMBER() OVER (
             PARTITION BY event_id, email_normalized
             ORDER BY updated_at DESC NULLS LAST, id DESC
           ) AS rn
    FROM in_person_main_data_center_registrations
  ) ranked
  WHERE ranked.rn > 1
);

-- ---------------------------------------------------------------------------
-- 4) Single canonical registration per email per event (matches virtual MDC)
-- ---------------------------------------------------------------------------
ALTER TABLE in_person_main_data_center_registrations
  ADD CONSTRAINT uq_ip_mdc_event_email UNIQUE (event_id, email_normalized);
