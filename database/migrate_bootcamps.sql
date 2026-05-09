-- Bootcamps: events.kind extension + bootcamp_sessions (city + date + morning|evening slot).
-- Idempotent; safe to re-run.
--
-- Caveat: UNIQUE (event_id, city, bootcamp_on, slot) allows at most one morning and one
-- evening per city per calendar day. Supporting two morning sessions on the same day
-- would require dropping that constraint or adding a disambiguator (e.g. slot_index).

-- ---------------------------------------------------------------------------
-- 1) events.kind includes 'bootcamp'
-- ---------------------------------------------------------------------------
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_kind_check;

ALTER TABLE events
  ADD CONSTRAINT events_kind_check CHECK (kind IN ('in_person', 'virtual', 'bootcamp'));

-- ---------------------------------------------------------------------------
-- 2) bootcamp_sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bootcamp_sessions (
  id SERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  city TEXT NOT NULL,
  bootcamp_on DATE NOT NULL,
  slot TEXT NOT NULL CHECK (slot IN ('morning', 'evening')),
  scope_key TEXT GENERATED ALWAYS AS (
    city
    || '|'
    || lpad(extract(year FROM bootcamp_on)::int::text, 4, '0')
    || '-' || lpad(extract(month FROM bootcamp_on)::int::text, 2, '0')
    || '-' || lpad(extract(day FROM bootcamp_on)::int::text, 2, '0')
    || '|'
    || slot
  ) STORED,
  display_name TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_bootcamp_sessions_event_city_date_slot UNIQUE (event_id, city, bootcamp_on, slot)
);

CREATE INDEX IF NOT EXISTS idx_bootcamp_sessions_event_id ON bootcamp_sessions (event_id);

CREATE INDEX IF NOT EXISTS idx_bootcamp_sessions_scope_key ON bootcamp_sessions (scope_key);

CREATE OR REPLACE FUNCTION fn_bootcamp_sessions_set_display_name()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.display_name :=
    initcap(NEW.city)
    || to_char(NEW.bootcamp_on::timestamp, 'DD Mon YYYY')
    || ' · '
    || initcap(NEW.slot);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tr_bootcamp_sessions_display ON bootcamp_sessions;
CREATE TRIGGER tr_bootcamp_sessions_display
  BEFORE INSERT OR UPDATE OF city, bootcamp_on, slot
  ON bootcamp_sessions
  FOR EACH ROW
  EXECUTE PROCEDURE fn_bootcamp_sessions_set_display_name();

-- ---------------------------------------------------------------------------
-- 3) Seed default bootcamp parent event (resolve id via slug in app env if needed)
-- ---------------------------------------------------------------------------
INSERT INTO events (slug, name, kind)
VALUES ('bootcamps-default', 'Bootcamps', 'bootcamp')
ON CONFLICT (slug) DO NOTHING;
