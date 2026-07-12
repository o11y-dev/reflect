CREATE INDEX IF NOT EXISTS idx_steps_session_type ON steps(session_id, type);
CREATE INDEX IF NOT EXISTS idx_llm_calls_session_request_model ON llm_calls(session_id, request_model);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session_status ON tool_calls(session_id, status);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_session_status ON mcp_calls(session_id, status);
CREATE INDEX IF NOT EXISTS idx_raw_events_session_source_time ON raw_events(session_id, source_type, observed_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_session_kind ON graph_nodes(session_id, kind);
CREATE INDEX IF NOT EXISTS idx_graph_edges_session_kind ON graph_edges(session_id, kind);
CREATE INDEX IF NOT EXISTS idx_memories_session_type_seen ON memories(session_id, type, last_seen_at DESC);
