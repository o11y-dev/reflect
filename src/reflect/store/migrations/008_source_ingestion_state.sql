CREATE TABLE IF NOT EXISTS source_ingestion_state (
  source_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  modified_ns INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (source_id, source_type)
);
