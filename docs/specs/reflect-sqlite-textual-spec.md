# Reflect Spec. SQLite, Pydantic, Graph Tables, Textual, DB-backed Reporting

## Decision

Reflect should move to a local-first SQLite architecture with Pydantic logical models, SQL migrations as the physical schema, SQLite graph tables for relationships, and Textual as the live UI.

```text
OpenTelemetry hooks / local telemetry
  -> raw_events
  -> Pydantic validation and normalization
  -> canonical SQLite tables
  -> rollups / SQL views
  -> graph_nodes / graph_edges
  -> Textual TUI and browser-served report
  -> optional static HTML / JSON export
```

Runtime reporting must not load dashboard JSON files into memory. JSON files are input or output artifacts only. SQLite is the runtime state.

## CLI behavior

```bash
reflect
```

Starts the default Textual TUI.

```bash
reflect report
```

Starts the browser-served Textual app and replaces the current live local HTML dashboard server.

```bash
reflect export --format html --output report.html
reflect export --format json --output report.json
```

Creates offline export artifacts from SQLite. Export does not power the live report.

Compatibility with the old command behavior is not required because the project is still early.

## Goals

- Use SQLite as the only live reporting store.
- Use Textual for the terminal UI and browser-served live report.
- Stop loading report JSON files into memory for live dashboards.
- Preserve raw OpenTelemetry attributes for reprocessing.
- Promote stable fields into canonical typed tables.
- Support memory attributes from OpenTelemetry hooks.
- Support specs, requirements, evidence, memory, privacy, cost, tokens, tools, MCP, models, repos, files, and graph relationships.
- Keep privacy and redaction enforced before data is stored or exported.

## Non-goals

- No Kuzu.
- No Neo4j.
- No external graph DB by default.
- No hosted backend.
- No full prompt / response / tool payload storage by default.
- No live dashboard backed by JSON artifacts.

## Storage layers

```text
1. raw_events
   Immutable-ish source event store.

2. canonical tables
   Typed reporting model.

3. rollup tables / SQL views
   Fast dashboard reads.

4. graph tables
   Local relationship traversal.

5. metadata tables
   Migrations, sources, normalization runs, semantic convention versions.
```

This is intentionally expandable. New OpenTelemetry fields land first in `raw_events.attrs_json`. Stable fields can later be promoted to typed columns through migrations.

## Pydantic design

Pydantic owns the logical schema. SQL migrations own the physical schema.

Pydantic owns:

```text
validation
typed models
JSON schema generation
serialization
normalization contracts
```

SQL owns:

```text
indexes
foreign keys
constraints
partial indexes
physical performance
migrations
```

Suggested files:

```text
src/reflect/schema/base.py
src/reflect/schema/events.py
src/reflect/schema/entities.py
src/reflect/schema/llm.py
src/reflect/schema/tools.py
src/reflect/schema/mcp.py
src/reflect/schema/specs.py
src/reflect/schema/memory.py
src/reflect/schema/privacy.py
src/reflect/schema/graph.py
src/reflect/schema/rollups.py

src/reflect/store/base.py
src/reflect/store/sqlite.py
src/reflect/store/migrations/001_initial.sql
src/reflect/store/migrations/002_rollups.sql
src/reflect/store/migrations/003_graph.sql

src/reflect/views/overview.py
src/reflect/views/sessions.py
src/reflect/views/agents.py
src/reflect/views/models.py
src/reflect/views/tools.py
src/reflect/views/mcp.py
src/reflect/views/costs.py
src/reflect/views/specs.py
src/reflect/views/memory.py
src/reflect/views/privacy.py

src/reflect/tui/app.py
src/reflect/tui/screens/
```

Base model pattern:

```python
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ReflectModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class RawAttributes(BaseModel):
    model_config = ConfigDict(extra="allow")
```

Rule:

```text
Canonical models use extra="forbid".
Raw OpenTelemetry attribute containers use extra="allow".
```

## SQLite defaults

Every SQLite connection must apply:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA wal_autocheckpoint = 1000;
PRAGMA busy_timeout = 5000;
```

Maintenance command:

```sql
PRAGMA optimize;
```

Optional strict durability mode:

```text
reflect config set sqlite.strict_durability true
```

When enabled:

```sql
PRAGMA synchronous = FULL;
```

## Core schema

### raw_events

`raw_events` is required. It prevents schema mistakes from becoming data loss.

```sql
CREATE TABLE raw_events (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  event_type TEXT NOT NULL,
  trace_id TEXT,
  span_id TEXT,
  parent_span_id TEXT,
  session_id TEXT,
  observed_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  attrs_json TEXT NOT NULL DEFAULT '{}',
  body_json TEXT NOT NULL DEFAULT '{}',
  normalized_status TEXT NOT NULL DEFAULT 'pending',
  normalization_error TEXT,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_raw_events_source_hash
ON raw_events(source_id, content_hash);

CREATE INDEX idx_raw_events_session_time
ON raw_events(session_id, observed_at);

CREATE INDEX idx_raw_events_status
ON raw_events(normalized_status);

CREATE INDEX idx_raw_events_trace_span
ON raw_events(trace_id, span_id);
```

### agents, repos, files

```sql
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT,
  version TEXT,
  raw_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE repos (
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

CREATE TABLE files (
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

CREATE INDEX idx_files_repo_path ON files(repo_id, path);
CREATE INDEX idx_files_repo_language ON files(repo_id, language);
```

### sessions

```sql
CREATE TABLE sessions (
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

CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX idx_sessions_agent_started ON sessions(agent_id, started_at DESC);
CREATE INDEX idx_sessions_repo_started ON sessions(repo_id, started_at DESC);
CREATE INDEX idx_sessions_status_started ON sessions(status, started_at DESC);
```

### steps

```sql
CREATE TABLE steps (
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

CREATE INDEX idx_steps_session_seq ON steps(session_id, seq);
CREATE INDEX idx_steps_type_time ON steps(type, started_at DESC);
CREATE INDEX idx_steps_parent ON steps(parent_step_id);
```

## LLM, tools, MCP

### llm_calls

```sql
CREATE TABLE llm_calls (
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

CREATE INDEX idx_llm_calls_session_model ON llm_calls(session_id, response_model);
CREATE INDEX idx_llm_calls_model_cost ON llm_calls(response_model, estimated_cost_usd DESC);
CREATE INDEX idx_llm_calls_provider_model ON llm_calls(provider, response_model);
```

### tool_calls

```sql
CREATE TABLE tool_calls (
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

CREATE INDEX idx_tool_calls_session_tool ON tool_calls(session_id, tool_name);
CREATE INDEX idx_tool_calls_tool_status ON tool_calls(tool_name, status);
CREATE INDEX idx_tool_calls_duration ON tool_calls(tool_name, duration_ms DESC);
```

### mcp_calls

```sql
CREATE TABLE mcp_calls (
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

CREATE INDEX idx_mcp_calls_session_server ON mcp_calls(session_id, server_name);
CREATE INDEX idx_mcp_calls_server_status ON mcp_calls(server_name, status);
```

## Specs, requirements, evidence

```sql
CREATE TABLE specs (
  id TEXT PRIMARY KEY,
  repo_id TEXT REFERENCES repos(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  owner TEXT,
  source_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE requirements (
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

CREATE TABLE evidence (
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

CREATE INDEX idx_requirements_spec_status ON requirements(spec_id, status);
CREATE INDEX idx_active_requirements ON requirements(spec_id, priority) WHERE status <> 'validated';
CREATE INDEX idx_evidence_requirement ON evidence(requirement_id);
CREATE INDEX idx_evidence_session ON evidence(session_id);
```

## Memory

Memory is first-class. OpenTelemetry hook attributes matching `gen_ai.memory.*` should be preserved in `raw_events.attrs_json`, promoted into `memories`, linked to sessions, steps, repos, files, specs, and evidence, and connected through graph edges.

```sql
CREATE TABLE memories (
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

CREATE INDEX idx_memories_scope_repo_type ON memories(scope, repo_id, type, last_seen_at DESC);
CREATE INDEX idx_memories_spec ON memories(spec_id, type, last_seen_at DESC);
CREATE INDEX idx_memories_session ON memories(session_id);
CREATE INDEX idx_live_memories ON memories(repo_id, type, last_seen_at DESC) WHERE expires_at IS NULL;
```

Pydantic type list for memory scope:

```text
global
workspace
repo
directory
file
spec
agent
user
team
```

Pydantic type list for memory type:

```text
repo_convention
validation_command
test_command
build_command
known_failure
successful_fix
architecture_note
style_preference
unsafe_pattern
tool_preference
spec_decision
deployment_rule
ownership_hint
unknown
```

## Privacy

Default storage mode is `metadata_only`.

Storage modes:

| Mode | Stored |
|---|---|
| `metadata_only` | IDs, timestamps, model names, tool names, token counts, costs, hashes, statuses |
| `redacted_preview` | metadata plus short redacted previews |
| `full_local` | full content local only, never exported by default |
| `full_encrypted` | full content encrypted at rest, explicit unlock required |

```sql
CREATE TABLE privacy_findings (
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

CREATE INDEX idx_privacy_session_severity ON privacy_findings(session_id, severity);
CREATE INDEX idx_privacy_type_severity ON privacy_findings(finding_type, severity);
```

Static HTML and JSON exports exclude raw prompts, responses, tool inputs, tool outputs, and file contents by default.

## Graph tables

Graph is local and SQLite-backed.

```sql
CREATE TABLE graph_nodes (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  label TEXT NOT NULL,
  properties_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE graph_edges (
  id TEXT PRIMARY KEY,
  from_node_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
  to_node_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  properties_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_graph_nodes_type ON graph_nodes(type);
CREATE INDEX idx_graph_edges_from_type ON graph_edges(from_node_id, type);
CREATE INDEX idx_graph_edges_to_type ON graph_edges(to_node_id, type);
CREATE INDEX idx_graph_edges_type ON graph_edges(type);
```

Node types:

```text
Session
Step
Agent
Model
Tool
MCPServer
Repo
File
Command
Error
Spec
Requirement
Evidence
Memory
PrivacyFinding
CostBucket
TokenBucket
```

Edge types:

```text
HAS_STEP
USED_AGENT
USED_MODEL
CALLED_TOOL
CALLED_MCP_SERVER
IN_REPO
TOUCHED_FILE
READ_FILE
WROTE_FILE
RAN_COMMAND
FAILED_WITH
HAS_COST
HAS_TOKENS
WORKED_ON_SPEC
IMPLEMENTS_REQUIREMENT
VALIDATED_BY
SUPPORTED_BY
HAS_MEMORY
MENTIONS_FILE
MENTIONS_COMMAND
HAS_PRIVACY_FINDING
FOLLOWED_BY
CO_OCCURRED_WITH
```

## Rollups

Textual must not scan all raw events for every screen.

```sql
CREATE TABLE session_rollups (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  step_count INTEGER NOT NULL DEFAULT 0,
  llm_call_count INTEGER NOT NULL DEFAULT 0,
  tool_call_count INTEGER NOT NULL DEFAULT 0,
  mcp_call_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  first_event_at TEXT,
  last_event_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE daily_rollups (
  day TEXT NOT NULL,
  agent_id TEXT,
  repo_id TEXT,
  model_id TEXT,
  session_count INTEGER NOT NULL DEFAULT 0,
  llm_call_count INTEGER NOT NULL DEFAULT 0,
  tool_call_count INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(day, agent_id, repo_id, model_id)
);

CREATE TABLE tool_rollups (
  tool_name TEXT NOT NULL,
  day TEXT NOT NULL,
  call_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  avg_duration_ms REAL,
  p95_duration_ms REAL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tool_name, day)
);
```

Rollups can be rebuilt with:

```bash
reflect db rebuild-rollups
```

## Metadata tables

```sql
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE ingest_sources (
  id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  path_or_endpoint TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE normalization_runs (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,
  source_id TEXT,
  raw_events_seen INTEGER NOT NULL DEFAULT 0,
  raw_events_normalized INTEGER NOT NULL DEFAULT 0,
  raw_events_failed INTEGER NOT NULL DEFAULT 0,
  error_redacted TEXT
);

CREATE TABLE semantic_convention_versions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT,
  status TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
```

## Textual screens

Required screens:

```text
Overview
Activity
Sessions
Session Detail
Agents
Models
Tools
MCP
Costs
Graphs
Observations
Specs
Memory
Privacy
Exports
Settings
```

UI rule:

```text
Textual screen
  -> view model
  -> SQL query
  -> SQLite
```

Textual screens must not read raw JSON files. Textual screens must not build metrics from in-memory dashboard artifacts.

## Migration plan

### Phase 1. Add schema package

- Add Pydantic models.
- Add model tests.
- Add JSON Schema generation command.

```bash
reflect schema export --output docs/schema/reflect.schema.json
```

### Phase 2. Add SQLite store

- Add migrations.
- Add DB connection wrapper.
- Enable WAL.
- Enable foreign keys per connection.
- Add DB doctor.

```bash
reflect db migrate
reflect doctor
```

### Phase 3. Add raw_events ingestion

- Store all source events in `raw_events`.
- Deduplicate by `source_id + content_hash`.
- Stop live JSON dashboard loading.

### Phase 4. Add normalization

Normalize:

```text
raw_events -> canonical tables
raw_events -> memories
raw_events -> graph tables
raw_events -> privacy findings
```

### Phase 5. Add rollups

- `session_rollups`
- `daily_rollups`
- `tool_rollups`

### Phase 6. Port Textual UI to SQL

- `reflect` opens Textual TUI.
- All screens read SQLite through view models.
- Long queries use Textual workers.
- Tables are paginated.

### Phase 7. Replace report command

- `reflect report` starts browser-served Textual.
- Old live HTML server is removed.

### Phase 8. Static export

- `reflect export --format html`
- `reflect export --format json`

Both query SQLite directly.

### Phase 9. Remove JSON runtime dependency

Delete or quarantine code paths that:

```text
load dashboard JSON
build full report object in memory
serve live HTML from JSON
```

Keep JSON only for:

```text
legacy importer
static exporter
tests
```

## Success criteria

### Runtime

- `reflect` opens Textual TUI by default.
- `reflect report` opens browser-served Textual.
- No live Textual screen reads dashboard JSON files.
- No live report path loads report JSON into memory.
- All report data comes from SQLite queries.

### DB correctness

- `reflect db migrate` creates a valid DB from empty state.
- `PRAGMA foreign_key_check` returns zero rows.
- `raw_events` deduplicates by `source_id + content_hash`.
- Normalization can be rerun safely.
- Rollups can be rebuilt from canonical tables.
- Graph nodes and edges are recreated idempotently.

### Pydantic correctness

- All canonical writes pass Pydantic validation.
- Raw OpenTelemetry attributes allow unknown fields.
- Canonical models reject unknown fields.
- Pydantic JSON Schema can be generated.
- Tests verify required Pydantic fields map to DB columns.

### Reporting parity

Old report tabs must have SQL-backed equivalents:

```text
Overview
Activity
Sessions
Agents / Compare
Models
Tools
MCP
Costs
Graphs
Observations
```

New tabs:

```text
Specs
Memory
Privacy
Exports
```

### Privacy

- Default mode is `metadata_only`.
- Raw prompt text is not stored by default.
- Raw response text is not stored by default.
- Raw tool input/output is not stored by default.
- Redacted previews are capped.
- Static export excludes sensitive content by default.
- Privacy findings are visible in the Privacy screen.

### Performance

For a local DB with at least:

```text
10,000 sessions
250,000 steps
100,000 LLM calls
250,000 tool calls
```

Targets:

```text
Overview loads in under 1 second from rollups.
Sessions first page loads in under 500 ms.
Session detail loads in under 500 ms.
Tool list loads in under 1 second.
Graph neighborhood query loads in under 1 second for one selected node.
Long queries run in Textual workers and do not block UI input.
```

### Migration

- Existing demo data can be imported once.
- Imported report aggregates match old dashboard totals within expected rounding.
- After import, UI reads SQLite only.
- Old JSON files are no longer required for report viewing.

## Final implementation rule

```text
JSON files are input or output artifacts only.
SQLite is the runtime state.
Pydantic is the logical contract.
SQL migrations are the physical contract.
Textual is the live UI.
```

This keeps Reflect local-first, expandable, testable, and not locked into the current dashboard artifact shape.
