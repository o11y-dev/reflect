ALTER TABLE source_ingestion_state
  ADD COLUMN record_cursor_time INTEGER;

ALTER TABLE source_ingestion_state
  ADD COLUMN record_cursor_id TEXT;
