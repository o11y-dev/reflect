CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT,
  version TEXT,
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
  id TEXT PRIMARY KEY,
  provider TEXT,
  owner TEXT,
  name TEXT,
  full_name TEXT NOT NULL UNIQUE,
  branch TEXT,
  commit_sha TEXT,
  dirty INTEGER NOT NULL DEFAULT 0,
  path_hash TEXT,
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  path_hash TEXT,
  extension TEXT,
  language TEXT,
  role TEXT,
  read_count INTEGER NOT NULL DEFAULT 0,
  write_count INTEGER NOT NULL DEFAULT 0,
  tokens_in_context INTEGER NOT NULL DEFAULT 0,
  sensitivity TEXT NOT NULL DEFAULT 'unknown',
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(repo_id, path)
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  agent_id TEXT REFERENCES agents(id),
  repo_id TEXT REFERENCES repos(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  title TEXT,
  quality_score REAL,
  failure_count INTEGER NOT NULL DEFAULT 0,
  recovered_failure_count INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  privacy_mode TEXT NOT NULL DEFAULT 'metadata_only',
  source_kind TEXT,
  source_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  parent_step_id TEXT REFERENCES steps(id),
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  duration_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'unknown',
  summary TEXT,
  raw_attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  provider TEXT,
  request_model TEXT,
  response_model TEXT,
  operation_name TEXT,
  finish_reason TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  latency_ms INTEGER,
  prompt_hash TEXT,
  response_hash TEXT,
  prompt_preview_redacted TEXT,
  response_preview_redacted TEXT,
  raw_attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  tool_name TEXT NOT NULL,
  tool_type TEXT,
  mcp_session_id TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  duration_ms INTEGER,
  input_hash TEXT,
  output_hash TEXT,
  input_preview_redacted TEXT,
  output_preview_redacted TEXT,
  error_type TEXT,
  error_message_redacted TEXT,
  raw_attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_calls (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  mcp_session_id TEXT,
  mcp_protocol_version TEXT,
  transport TEXT,
  server_name TEXT,
  tool_name TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  duration_ms INTEGER,
  raw_attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specs (
  id TEXT PRIMARY KEY,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  owner TEXT,
  source_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirements (
  id TEXT PRIMARY KEY,
  spec_id TEXT NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'planned',
  priority TEXT NOT NULL DEFAULT 'medium',
  evidence_status TEXT NOT NULL DEFAULT 'missing',
  confidence REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  requirement_id TEXT REFERENCES requirements(id) ON DELETE CASCADE,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  step_id TEXT REFERENCES steps(id) ON DELETE SET NULL,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  file_id TEXT REFERENCES files(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,
  summary TEXT,
  confidence REAL NOT NULL DEFAULT 0,
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  type TEXT NOT NULL,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  file_id TEXT REFERENCES files(id) ON DELETE SET NULL,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  step_id TEXT REFERENCES steps(id) ON DELETE SET NULL,
  spec_id TEXT REFERENCES specs(id) ON DELETE SET NULL,
  content_hash TEXT,
  content_preview_redacted TEXT,
  confidence REAL NOT NULL DEFAULT 0.5,
  sensitivity TEXT NOT NULL DEFAULT 'unknown',
  source TEXT NOT NULL,
  expires_at TEXT,
  last_seen_at TEXT,
  raw_attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS privacy_findings (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
  step_id TEXT REFERENCES steps(id) ON DELETE SET NULL,
  finding_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  field_name TEXT,
  action_taken TEXT NOT NULL,
  detail_redacted TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_repo_path ON files(repo_id, path);
CREATE INDEX IF NOT EXISTS idx_files_repo_language ON files(repo_id, language);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_started ON sessions(agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_repo_started ON sessions(repo_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status_started ON sessions(status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_steps_session_seq ON steps(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_steps_type_time ON steps(type, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_steps_parent ON steps(parent_step_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_session_model ON llm_calls(session_id, response_model);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model_cost ON llm_calls(response_model, estimated_cost_usd DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_provider_model ON llm_calls(provider, response_model);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session_tool ON tool_calls(session_id, tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_status ON tool_calls(tool_name, status);
CREATE INDEX IF NOT EXISTS idx_tool_calls_duration ON tool_calls(tool_name, duration_ms DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_session_server ON mcp_calls(session_id, server_name);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_server_status ON mcp_calls(server_name, status);
CREATE INDEX IF NOT EXISTS idx_requirements_spec_status ON requirements(spec_id, status);
CREATE INDEX IF NOT EXISTS idx_active_requirements ON requirements(spec_id, priority) WHERE status <> 'validated';
CREATE INDEX IF NOT EXISTS idx_evidence_requirement ON evidence(requirement_id);
CREATE INDEX IF NOT EXISTS idx_evidence_session ON evidence(session_id);
CREATE INDEX IF NOT EXISTS idx_memories_scope_repo_type ON memories(scope, repo_id, type, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_spec ON memories(spec_id, type, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_live_memories ON memories(repo_id, type, last_seen_at DESC) WHERE expires_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_privacy_session_severity ON privacy_findings(session_id, severity);
CREATE INDEX IF NOT EXISTS idx_privacy_type_severity ON privacy_findings(finding_type, severity);
