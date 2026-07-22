CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  root_path TEXT NOT NULL,
  path_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  source_key TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

ALTER TABLE sessions ADD COLUMN workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL;
ALTER TABLE sessions ADD COLUMN parent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL;
ALTER TABLE graph_nodes ADD COLUMN identity_key TEXT NOT NULL DEFAULT '';

DROP INDEX IF EXISTS idx_graph_nodes_kind_label_session;

CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_kind_identity
  ON graph_nodes(kind, identity_key)
  WHERE identity_key <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_kind_label_session_identity
  ON graph_nodes(kind, label, COALESCE(session_id, ''), identity_key);
CREATE INDEX IF NOT EXISTS idx_workspaces_repo ON workspaces(repo_id, label);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace_started ON sessions(workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_parent_started ON sessions(parent_session_id, started_at DESC);

-- The semantic graph is derived state. Rebuild it once under the new identity
-- contract so old session-local folder/path nodes and noisy preview paths do not
-- survive the migration.
DELETE FROM graph_edges;
DELETE FROM graph_nodes;
