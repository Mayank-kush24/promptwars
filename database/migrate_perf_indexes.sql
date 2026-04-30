-- Optional performance indexes for Main Data Center date-range queries.
-- Run during a maintenance window; CONCURRENTLY avoids blocking writes.
-- psql "$DATABASE_URL" -f database/migrate_perf_indexes.sql

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_in_person_mdc_event_form_ts
  ON in_person_main_data_center_registrations (event_id, form_timestamp);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_virtual_mdc_event_form_ts
  ON virtual_main_data_center_registrations (event_id, form_timestamp);
