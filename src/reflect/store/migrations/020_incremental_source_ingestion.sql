ALTER TABLE source_ingestion_state
  ADD COLUMN processed_offset_bytes INTEGER NOT NULL DEFAULT 0;

ALTER TABLE source_ingestion_state
  ADD COLUMN checkpoint_tail_sha256 TEXT;
