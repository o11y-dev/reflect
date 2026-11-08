-- The graph is derived state. Canonical telemetry and usage remain untouched.
-- Replace display-label identities and remove redundant per-step topology.
DELETE FROM graph_edges;
DELETE FROM graph_nodes;

CREATE TABLE graph_edge_sources (
  edge_id TEXT NOT NULL REFERENCES graph_edges(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('step', 'tool_call')),
  source_id TEXT NOT NULL,
  source_order TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1,
  first_seen_at TEXT,
  last_seen_at TEXT,
  attrs_json TEXT NOT NULL,
  PRIMARY KEY(edge_id, source_kind, source_id)
);
CREATE INDEX idx_graph_edge_sources_owner ON graph_edge_sources(source_kind, source_id);
CREATE INDEX idx_graph_edge_sources_order ON graph_edge_sources(edge_id, source_order);
CREATE INDEX idx_tool_calls_graph_recent ON tool_calls(created_at DESC,id DESC);
CREATE INDEX idx_tool_calls_graph_session_recent ON tool_calls(session_id,created_at DESC,id DESC);

INSERT INTO pending_refresh(phase, session_id, revision)
SELECT 'graph', id, 'compact-ontology-v2' FROM sessions WHERE 1
ON CONFLICT(phase,session_id) DO NOTHING;
