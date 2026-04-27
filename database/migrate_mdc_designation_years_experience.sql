-- Split trailing "( N )" / "( N)" years marker from designation into designation_years_experience.
-- Safe to run multiple times: only updates rows that still match the trailing-years pattern.

ALTER TABLE in_person_main_data_center_registrations
  ADD COLUMN IF NOT EXISTS designation_years_experience INTEGER;

ALTER TABLE virtual_main_data_center_registrations
  ADD COLUMN IF NOT EXISTS designation_years_experience INTEGER;

UPDATE in_person_main_data_center_registrations
SET
  designation_years_experience = (substring(designation from '\(\s*(\d+)\s*\)\s*$'))::integer,
  designation = NULLIF(regexp_replace(designation, '\s*\(\s*\d+\s*\)\s*$', ''), '')
WHERE designation IS NOT NULL
  AND designation ~ '\(\s*\d+\s*\)\s*$';

UPDATE virtual_main_data_center_registrations
SET
  designation_years_experience = (substring(designation from '\(\s*(\d+)\s*\)\s*$'))::integer,
  designation = NULLIF(regexp_replace(designation, '\s*\(\s*\d+\s*\)\s*$', ''), '')
WHERE designation IS NOT NULL
  AND designation ~ '\(\s*\d+\s*\)\s*$';
