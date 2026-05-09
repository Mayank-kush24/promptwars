-- Bootcamp session numeric metrics (per city + date + slot).
-- Idempotent; safe to re-run.

ALTER TABLE bootcamp_sessions
  ADD COLUMN IF NOT EXISTS attendees_count INTEGER NOT NULL DEFAULT 0 CHECK (attendees_count >= 0),
  ADD COLUMN IF NOT EXISTS activations_count INTEGER NOT NULL DEFAULT 0 CHECK (activations_count >= 0),
  ADD COLUMN IF NOT EXISTS students_count INTEGER NOT NULL DEFAULT 0 CHECK (students_count >= 0),
  ADD COLUMN IF NOT EXISTS professionals_count INTEGER NOT NULL DEFAULT 0 CHECK (professionals_count >= 0),
  ADD COLUMN IF NOT EXISTS metrics_updated_at TIMESTAMPTZ;
