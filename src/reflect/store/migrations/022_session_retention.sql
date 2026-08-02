ALTER TABLE sessions ADD COLUMN last_observed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_last_observed
  ON sessions(last_observed_at DESC);

CREATE TABLE IF NOT EXISTS pruned_sessions (
  id TEXT PRIMARY KEY,
  agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
  agent_name TEXT,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
  source_kind TEXT,
  source_ref TEXT,
  source_hash TEXT,
  started_at TEXT,
  last_observed_at TEXT,
  status TEXT,
  pruned_at TEXT NOT NULL,
  prune_reason TEXT NOT NULL,
  deleted_counts_json TEXT NOT NULL DEFAULT '{}',
  resurrected_at TEXT,
  newer_observed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pruned_sessions_source
  ON pruned_sessions(source_kind, source_ref);
CREATE INDEX IF NOT EXISTS idx_pruned_sessions_last_observed
  ON pruned_sessions(last_observed_at DESC);

ALTER TABLE session_outcomes RENAME TO session_outcomes_before_retention;

CREATE TABLE session_outcomes (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  pruned_session_id TEXT REFERENCES pruned_sessions(id) ON DELETE SET NULL,
  outcome TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  verification_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (session_id, source)
);

INSERT INTO session_outcomes(
  id, session_id, outcome, source, confidence, verification_json,
  created_at, updated_at
)
SELECT id, session_id, outcome, source, confidence, verification_json,
       created_at, updated_at
FROM session_outcomes_before_retention;

DROP TABLE session_outcomes_before_retention;

CREATE INDEX IF NOT EXISTS idx_session_outcomes_pruned_session
  ON session_outcomes(pruned_session_id);

ALTER TABLE operator_feedback RENAME TO operator_feedback_before_retention;

CREATE TABLE operator_feedback (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  pruned_session_id TEXT REFERENCES pruned_sessions(id) ON DELETE SET NULL,
  outcome TEXT NOT NULL,
  reason_redacted TEXT,
  actor TEXT NOT NULL DEFAULT 'local_operator',
  created_at TEXT NOT NULL
);

INSERT INTO operator_feedback(
  id, session_id, outcome, reason_redacted, actor, created_at
)
SELECT id, session_id, outcome, reason_redacted, actor, created_at
FROM operator_feedback_before_retention;

DROP TABLE operator_feedback_before_retention;

CREATE INDEX IF NOT EXISTS idx_operator_feedback_pruned_session
  ON operator_feedback(pruned_session_id);
CREATE INDEX IF NOT EXISTS idx_operator_feedback_session
  ON operator_feedback(session_id, created_at DESC);

ALTER TABLE memories
  ADD COLUMN pruned_session_id TEXT REFERENCES pruned_sessions(id) ON DELETE SET NULL;
ALTER TABLE evidence
  ADD COLUMN pruned_session_id TEXT REFERENCES pruned_sessions(id) ON DELETE SET NULL;
ALTER TABLE observation_evidence
  ADD COLUMN pruned_session_id TEXT REFERENCES pruned_sessions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_memories_pruned_session
  ON memories(pruned_session_id);
CREATE INDEX IF NOT EXISTS idx_evidence_pruned_session
  ON evidence(pruned_session_id);
CREATE INDEX IF NOT EXISTS idx_observation_evidence_pruned_session
  ON observation_evidence(pruned_session_id);

UPDATE sessions AS target
SET started_at = COALESCE(
  (
    SELECT MIN(candidate_at)
    FROM (
      SELECT observed_at AS candidate_at
      FROM raw_events
      WHERE session_id = target.id
        AND observed_at <> ''
        AND substr(observed_at, 1, 4) >= '2000'
      UNION ALL
      SELECT started_at
      FROM steps
      WHERE session_id = target.id
        AND started_at <> ''
        AND substr(started_at, 1, 4) >= '2000'
      UNION ALL
      SELECT target.ended_at
      WHERE target.ended_at IS NOT NULL
        AND target.ended_at <> ''
        AND substr(target.ended_at, 1, 4) >= '2000'
    )
  ),
  target.started_at
)
WHERE target.started_at IS NULL
   OR target.started_at = ''
   OR substr(target.started_at, 1, 4) < '2000';

UPDATE sessions AS target
SET last_observed_at = (
  SELECT MAX(candidate_at)
  FROM (
    SELECT observed_at AS candidate_at
    FROM raw_events
    WHERE session_id = target.id
      AND observed_at <> ''
      AND substr(observed_at, 1, 4) >= '2000'
    UNION ALL
    SELECT COALESCE(NULLIF(ended_at, ''), started_at)
    FROM steps
    WHERE session_id = target.id
      AND COALESCE(NULLIF(ended_at, ''), started_at) <> ''
      AND substr(COALESCE(NULLIF(ended_at, ''), started_at), 1, 4) >= '2000'
    UNION ALL
    SELECT target.ended_at
    WHERE target.ended_at IS NOT NULL
      AND target.ended_at <> ''
      AND substr(target.ended_at, 1, 4) >= '2000'
    UNION ALL
    SELECT target.started_at
    WHERE target.started_at IS NOT NULL
      AND target.started_at <> ''
      AND substr(target.started_at, 1, 4) >= '2000'
  )
);

UPDATE observation_sessions
SET latest_source_at = COALESCE(
  (SELECT s.last_observed_at FROM sessions s WHERE s.id = observation_sessions.session_id),
  latest_source_at
);

CREATE TABLE IF NOT EXISTS store_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT INTO store_metadata(key, value, updated_at)
VALUES (
  'observation_session_ledger_v1',
  CASE
    WHEN EXISTS (
      SELECT 1
      FROM observations o
      WHERE o.status NOT IN ('resolved', 'dismissed', 'rejected', 'rolled_back')
        AND o.affected_session_count > (
        SELECT COUNT(*)
        FROM observation_sessions os
        WHERE os.observation_id = o.id
      )
    )
    THEN 'pending_rebuild'
    ELSE 'complete'
  END,
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
)
ON CONFLICT(key) DO NOTHING;
