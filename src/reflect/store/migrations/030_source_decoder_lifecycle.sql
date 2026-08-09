ALTER TABLE source_ingestion_state
  ADD COLUMN decoder_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE source_ingestion_state
  ADD COLUMN normalized_at TEXT;

ALTER TABLE source_ingestion_state
  ADD COLUMN raw_deleted_at TEXT;

CREATE INDEX idx_source_ingestion_lifecycle
  ON source_ingestion_state(source_type, decoder_version, normalized_at);
