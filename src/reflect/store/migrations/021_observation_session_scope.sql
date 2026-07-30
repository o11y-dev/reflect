CREATE TABLE IF NOT EXISTS observation_sessions (
  observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  summary_redacted TEXT NOT NULL DEFAULT '',
  focus_entity_type TEXT,
  focus_entity_id TEXT,
  latest_source_at TEXT NOT NULL,
  attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (observation_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_observation_sessions_session
  ON observation_sessions(session_id, observation_id);
CREATE INDEX IF NOT EXISTS idx_observation_sessions_observation_latest
  ON observation_sessions(observation_id, latest_source_at DESC);

INSERT OR IGNORE INTO observation_sessions(
  observation_id, session_id, occurrence_count, summary_redacted,
  focus_entity_type, focus_entity_id, latest_source_at, attrs_json,
  created_at, updated_at
)
SELECT
  oe.observation_id,
  oe.session_id,
  COUNT(*) AS occurrence_count,
  MIN(oe.summary_redacted) AS summary_redacted,
  MIN(oe.entity_type) AS focus_entity_type,
  MIN(oe.entity_id) AS focus_entity_id,
  MAX(COALESCE(s.ended_at, s.started_at, oe.created_at)) AS latest_source_at,
  '{}',
  MIN(oe.created_at),
  MAX(oe.created_at)
FROM observation_evidence oe
JOIN sessions s ON s.id = oe.session_id
WHERE oe.session_id IS NOT NULL
GROUP BY oe.observation_id, oe.session_id;
