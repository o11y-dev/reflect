ALTER TABLE memories ADD COLUMN provider TEXT NOT NULL DEFAULT 'local_sqlite';
ALTER TABLE memories ADD COLUMN provider_memory_id TEXT;
ALTER TABLE memories ADD COLUMN provider_status TEXT NOT NULL DEFAULT 'stored';
ALTER TABLE memories ADD COLUMN validation_status TEXT NOT NULL DEFAULT 'unvalidated';
ALTER TABLE memories ADD COLUMN validated_at TEXT;
ALTER TABLE memories ADD COLUMN validation_error TEXT;
ALTER TABLE memories ADD COLUMN stale_reason TEXT;
ALTER TABLE memories ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}';

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  memory_id UNINDEXED,
  content,
  type,
  scope,
  source,
  path
);

CREATE TABLE IF NOT EXISTS memory_candidates (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  type TEXT NOT NULL,
  content_preview_redacted TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5,
  source_metadata_json TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'candidate',
  promoted_memory_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_provider_status
  ON memories(provider, provider_status, validation_status);
CREATE INDEX IF NOT EXISTS idx_memories_validation
  ON memories(validation_status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_status
  ON memory_candidates(status, updated_at DESC);
