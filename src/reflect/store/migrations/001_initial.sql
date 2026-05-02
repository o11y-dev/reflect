CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_events (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  event_type TEXT NOT NULL,
  trace_id TEXT,
  span_id TEXT,
  parent_span_id TEXT,
  session_id TEXT,
  observed_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  attrs_json TEXT NOT NULL DEFAULT '{}',
  body_json TEXT NOT NULL DEFAULT '{}',
  normalized_status TEXT NOT NULL DEFAULT 'pending',
  normalization_error TEXT,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_events_source_hash ON raw_events(source_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_events_session_time ON raw_events(session_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_raw_events_status ON raw_events(normalized_status);
CREATE INDEX IF NOT EXISTS idx_raw_events_trace_span ON raw_events(trace_id, span_id);
