-- Vision UTS → virtual_main_data_center_registrations sync checkpoint (one row per virtual event).
CREATE TABLE IF NOT EXISTS vision_uts_virtual_mdc_sync_state (
  event_id INTEGER PRIMARY KEY REFERENCES events (id) ON DELETE CASCADE,
  last_success_at TIMESTAMPTZ,
  last_run_started_at TIMESTAMPTZ,
  last_run_finished_at TIMESTAMPTZ,
  last_run_status TEXT,
  last_rows_fetched INTEGER,
  last_rows_inserted INTEGER,
  last_rows_updated INTEGER,
  last_rows_failed INTEGER,
  last_error TEXT,
  last_triggered_by TEXT,
  last_payload_digest TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vision_uts_vmdc_sync_state_updated
  ON vision_uts_virtual_mdc_sync_state (updated_at DESC);
