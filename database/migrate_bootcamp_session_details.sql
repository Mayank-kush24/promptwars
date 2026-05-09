-- Bootcamp session: full planning / logistics fields (text + audience/capacity counts).
-- Idempotent; safe to re-run.

ALTER TABLE bootcamp_sessions
  ADD COLUMN IF NOT EXISTS venue_status TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS speaker_status TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS topic TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS speaker_details TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS audience_size INTEGER NOT NULL DEFAULT 0 CHECK (audience_size >= 0),
  ADD COLUMN IF NOT EXISTS audience_type TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS location TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS complete_address TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS food_beverage TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS printables TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS design_link TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS deck_link TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS capacity INTEGER NOT NULL DEFAULT 0 CHECK (capacity >= 0);
