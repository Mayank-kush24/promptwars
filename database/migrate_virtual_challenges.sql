-- Optional one-time migration: add admin-friendly columns + touch trigger
-- to the existing `challenges` table (matches database/init.sql as of 2026-04).
--
-- Safe to re-run: every statement is idempotent.

-- 1) Additive columns
ALTER TABLE challenges
  ADD COLUMN IF NOT EXISTS description TEXT,
  ADD COLUMN IF NOT EXISTS slug TEXT,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- 2) Case-insensitive uniqueness on (event_id, title)
CREATE UNIQUE INDEX IF NOT EXISTS uq_challenges_event_title_lower
  ON challenges (event_id, lower(title));

-- 3) Touch trigger to keep updated_at fresh on UPDATE
CREATE OR REPLACE FUNCTION fn_challenges_touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS challenges_touch_updated_at ON challenges;
CREATE TRIGGER challenges_touch_updated_at
BEFORE UPDATE ON challenges
FOR EACH ROW
EXECUTE FUNCTION fn_challenges_touch_updated_at();

-- 4) Re-attach audit row trigger if database/audit.sql is installed.
--    Wrapped in DO block so it is a no-op when the audit schema is absent.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'audit' AND p.proname = 'install_data_change_trigger'
  ) THEN
    PERFORM audit.install_data_change_trigger('public.challenges'::regclass);
  END IF;
END
$$;
