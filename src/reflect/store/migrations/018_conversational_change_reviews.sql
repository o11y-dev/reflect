CREATE TABLE IF NOT EXISTS change_reviews (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  candidate_id TEXT NOT NULL REFERENCES workflow_candidates(id) ON DELETE CASCADE,
  project_root TEXT,
  target_path TEXT,
  previous_hash TEXT,
  proposed_hash TEXT,
  binding_hash TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  expires_at TEXT NOT NULL,
  applied_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_reviews_candidate_status
  ON change_reviews(candidate_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_change_reviews_expiry
  ON change_reviews(status, expires_at);
