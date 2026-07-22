ALTER TABLE steps ADD COLUMN hook_event_id TEXT;
ALTER TABLE steps ADD COLUMN hook_event_id_source TEXT;
ALTER TABLE steps ADD COLUMN telemetry_source TEXT;
ALTER TABLE steps ADD COLUMN hook_schema_version INTEGER;
ALTER TABLE steps ADD COLUMN hook_provider_adapter TEXT;
ALTER TABLE steps ADD COLUMN original_event TEXT;
ALTER TABLE steps ADD COLUMN native_trace_id TEXT;
ALTER TABLE steps ADD COLUMN native_span_id TEXT;
ALTER TABLE steps ADD COLUMN native_parent_span_id TEXT;
ALTER TABLE steps ADD COLUMN agent_invocation_id TEXT;
ALTER TABLE steps ADD COLUMN parent_agent_id TEXT;

CREATE TABLE conversation_facts (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  role TEXT NOT NULL,
  content_hash TEXT,
  content_length INTEGER NOT NULL DEFAULT 0,
  content_preview_redacted TEXT,
  raw_attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(step_id, kind, role)
);

CREATE TABLE agent_events (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL UNIQUE REFERENCES steps(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  event_name TEXT NOT NULL,
  event_id TEXT,
  agent_id TEXT,
  parent_agent_id TEXT,
  agent_type TEXT,
  agent_id_source TEXT,
  status TEXT,
  task_hash TEXT,
  task_length INTEGER NOT NULL DEFAULT 0,
  task_preview_redacted TEXT,
  raw_attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_steps_hook_event_id
  ON steps(session_id, hook_event_id)
  WHERE hook_event_id IS NOT NULL AND hook_event_id <> '';
CREATE INDEX idx_steps_hook_contract
  ON steps(telemetry_source, hook_schema_version, started_at DESC);
CREATE INDEX idx_steps_native_context
  ON steps(native_trace_id, native_span_id)
  WHERE native_trace_id IS NOT NULL AND native_trace_id <> '';
CREATE INDEX idx_conversation_facts_session_kind
  ON conversation_facts(session_id, kind, created_at);
CREATE INDEX idx_agent_events_session_agent
  ON agent_events(session_id, agent_id, event_name);
CREATE INDEX idx_agent_events_parent
  ON agent_events(parent_agent_id, event_name)
  WHERE parent_agent_id IS NOT NULL AND parent_agent_id <> '';

UPDATE steps
SET hook_event_id = json_extract(raw_attrs_json, '$."gen_ai.client.hook.event_id"'),
    hook_event_id_source = json_extract(
      raw_attrs_json,
      '$."gen_ai.client.hook.event_id_source"'
    ),
    telemetry_source = json_extract(raw_attrs_json, '$."gen_ai.client.telemetry_source"'),
    hook_schema_version = json_extract(
      raw_attrs_json,
      '$."gen_ai.client.hook_schema_version"'
    ),
    hook_provider_adapter = json_extract(
      raw_attrs_json,
      '$."gen_ai.client.hook.provider_adapter"'
    ),
    original_event = json_extract(raw_attrs_json, '$."gen_ai.client.hook.original_event"'),
    native_trace_id = json_extract(raw_attrs_json, '$."gen_ai.client.native_trace_id"'),
    native_span_id = json_extract(raw_attrs_json, '$."gen_ai.client.native_span_id"'),
    native_parent_span_id = json_extract(
      raw_attrs_json,
      '$."gen_ai.client.native_parent_span_id"'
    ),
    agent_invocation_id = COALESCE(
      json_extract(raw_attrs_json, '$."gen_ai.client.agent_id"'),
      json_extract(raw_attrs_json, '$."gen_ai.agent.id"')
    ),
    parent_agent_id = json_extract(raw_attrs_json, '$."gen_ai.client.parent_agent_id"');

INSERT OR IGNORE INTO conversation_facts(
  id, step_id, session_id, kind, role, content_hash, content_length,
  content_preview_redacted, raw_attrs_json, created_at, updated_at
)
SELECT
  'message_' || replace(step.id, ':', '_') || '_' || replace(fact.kind, '.', '_'),
  step.id,
  step.session_id,
  fact.kind,
  fact.role,
  json_extract(step.raw_attrs_json, '$."gen_ai.client.' || fact.kind || '.sha256"'),
  COALESCE(
    json_extract(step.raw_attrs_json, '$."gen_ai.client.' || fact.kind || '.length"'),
    0
  ),
  json_extract(step.raw_attrs_json, '$."gen_ai.client.' || fact.kind || '.text"'),
  step.raw_attrs_json,
  step.created_at,
  step.updated_at
FROM steps step
CROSS JOIN (
  SELECT 'prompt' AS kind, 'user' AS role
  UNION ALL SELECT 'response', 'assistant'
  UNION ALL SELECT 'stop_message', 'system'
  UNION ALL SELECT 'error', 'error'
  UNION ALL SELECT 'delegation.task', 'system'
) fact
WHERE json_extract(step.raw_attrs_json, '$."gen_ai.client.' || fact.kind || '.sha256"')
        IS NOT NULL
   OR json_extract(step.raw_attrs_json, '$."gen_ai.client.' || fact.kind || '.length"')
        IS NOT NULL
   OR json_extract(step.raw_attrs_json, '$."gen_ai.client.' || fact.kind || '.text"')
        IS NOT NULL;

INSERT OR IGNORE INTO agent_events(
  id, step_id, session_id, event_name, event_id, agent_id, parent_agent_id,
  agent_type, agent_id_source, status, task_hash, task_length,
  task_preview_redacted, raw_attrs_json, created_at, updated_at
)
SELECT
  'agent_event_' || replace(id, ':', '_'),
  id,
  session_id,
  COALESCE(json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'), summary),
  json_extract(raw_attrs_json, '$."gen_ai.client.hook.event_id"'),
  COALESCE(
    json_extract(raw_attrs_json, '$."gen_ai.client.agent_id"'),
    json_extract(raw_attrs_json, '$."gen_ai.agent.id"')
  ),
  json_extract(raw_attrs_json, '$."gen_ai.client.parent_agent_id"'),
  COALESCE(
    json_extract(raw_attrs_json, '$."gen_ai.client.subagent_type"'),
    json_extract(raw_attrs_json, '$."gen_ai.agent.name"')
  ),
  json_extract(raw_attrs_json, '$."gen_ai.client.agent_id_source"'),
  json_extract(raw_attrs_json, '$."gen_ai.client.status"'),
  json_extract(raw_attrs_json, '$."gen_ai.client.delegation.task.sha256"'),
  COALESCE(json_extract(raw_attrs_json, '$."gen_ai.client.delegation.task.length"'), 0),
  json_extract(raw_attrs_json, '$."gen_ai.client.delegation.task.text"'),
  raw_attrs_json,
  created_at,
  updated_at
FROM steps
WHERE lower(COALESCE(json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'), summary, ''))
        IN ('subagentstart', 'subagentstop')
   OR lower(COALESCE(json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'), summary, ''))
        LIKE '%.subagentstart'
   OR lower(COALESCE(json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'), summary, ''))
        LIKE '%.subagentstop';
