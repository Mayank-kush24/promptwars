-- Hawkeye RSVP snapshots + generic external event bridge.
--
-- Purpose: map Prompt Wars ``events.id`` (and per-PW-session sub-scopes such as
-- city + date + label) to third-party identifiers (e.g. Hawkeye ``eventTag``)
-- and store time-series API payloads (denormalized headline columns + full
-- JSONB for forensics and future dashboards).
--
-- Generic pattern: ``event_external_mappings`` is shared by future integrations
-- (Vision MDC API, Vision Action Center API, etc.) via ``system_name`` +
-- ``external_key``. Each vendor may add a dedicated ``*_snapshots`` table shaped
-- like ``hawkeye_rsvp_snapshots`` (event_id, mapping_id, headline metrics, raw JSONB).
--
-- ``scope_key`` lets one ``event_id`` host many mappings -- e.g. one mapping per
-- in-person Prompt War session ``"<city_lower>|<YYYY-MM-DD>|<label>"`` while a
-- top-level event mapping uses ``''`` (the empty string).
--
-- Idempotent; safe to re-run.

CREATE TABLE IF NOT EXISTS event_external_mappings (
  id SERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  system_name TEXT NOT NULL,
  external_key TEXT NOT NULL,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_event_external_mappings_event_system UNIQUE (event_id, system_name)
);

CREATE INDEX IF NOT EXISTS idx_event_external_mappings_system_key
  ON event_external_mappings (system_name, external_key);

-- Sub-scope (e.g. PW session) support: scope_key is '' for top-level event mappings.
ALTER TABLE event_external_mappings
  ADD COLUMN IF NOT EXISTS scope_key TEXT NOT NULL DEFAULT '';
ALTER TABLE event_external_mappings
  ADD COLUMN IF NOT EXISTS scope JSONB;

-- Replace UNIQUE (event_id, system_name) with UNIQUE (event_id, system_name, scope_key).
ALTER TABLE event_external_mappings
  DROP CONSTRAINT IF EXISTS uq_event_external_mappings_event_system;
DO $eem_uq$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'uq_event_external_mappings_event_system_scope'
  ) THEN
    ALTER TABLE event_external_mappings
      ADD CONSTRAINT uq_event_external_mappings_event_system_scope
      UNIQUE (event_id, system_name, scope_key);
  END IF;
END
$eem_uq$;

CREATE TABLE IF NOT EXISTS hawkeye_rsvp_snapshots (
  id SERIAL PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
  mapping_id INTEGER REFERENCES event_external_mappings (id) ON DELETE SET NULL,
  hawkeye_event_id TEXT,
  hawkeye_event_tag TEXT,
  hawkeye_event_name TEXT,
  rsvp_invite_sent INTEGER,
  rsvp_accepted INTEGER,
  checked_in_participants INTEGER,
  raw_stats JSONB,
  raw_stats_emails JSONB,
  raw_event_meta JSONB,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fetch_triggered_by TEXT
);

ALTER TABLE hawkeye_rsvp_snapshots
  ADD COLUMN IF NOT EXISTS scope_key TEXT NOT NULL DEFAULT '';

DROP INDEX IF EXISTS idx_hawkeye_rsvp_snapshots_event_fetched;
CREATE INDEX IF NOT EXISTS idx_hawkeye_rsvp_snapshots_event_scope_fetched
  ON hawkeye_rsvp_snapshots (event_id, scope_key, fetched_at DESC);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'audit' AND p.proname = 'install_data_change_trigger'
  ) THEN
    PERFORM audit.install_data_change_trigger('public.event_external_mappings'::regclass);
    PERFORM audit.install_data_change_trigger('public.hawkeye_rsvp_snapshots'::regclass);
  END IF;
END
$$;
