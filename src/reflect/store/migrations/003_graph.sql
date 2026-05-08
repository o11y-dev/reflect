CREATE TABLE IF NOT EXISTS graph_nodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  session_id TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_edges (
  id TEXT PRIMARY KEY,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  session_id TEXT,
  weight REAL NOT NULL DEFAULT 1,
  first_seen_at TEXT,
  last_seen_at TEXT,
  attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
  FOREIGN KEY (target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_kind_label_session
  ON graph_nodes(kind, label, COALESCE(session_id, ''));
CREATE INDEX IF NOT EXISTS idx_graph_nodes_session ON graph_nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_node_id, kind);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_node_id, kind);
CREATE INDEX IF NOT EXISTS idx_graph_edges_session ON graph_edges(session_id);
