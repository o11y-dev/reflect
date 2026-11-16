-- Rank compact graph metadata before fetching bounded evidence payloads.
CREATE INDEX idx_graph_nodes_rank ON graph_nodes(
  session_id, kind, last_seen_at, first_seen_at, label, id
);
CREATE INDEX idx_graph_edges_rank ON graph_edges(
  session_id, kind, weight, last_seen_at, first_seen_at, id
);
