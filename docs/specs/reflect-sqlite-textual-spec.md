# Reflect Spec. SQLite, Pydantic, Graph Tables, Textual, DB-backed Reporting

Status: implementation brief for Codex
Target repo: `o11y-dev/reflect`

## 1. Executive decision

Reflect will move from report JSON and in-memory dashboard state to a local-first SQLite architecture.

```text
OpenTelemetry hooks / local agent telemetry
  -> raw_events
  -> Pydantic validation and normalization
  -> canonical SQLite tables
  -> rollup tables / SQL views
  -> graph_nodes / graph_edges
  -> Textual TUI and browser-served report
  -> optional static HTML / JSON export
```

Runtime reporting must not load dashboard JSON files into memory. JSON files are input or output artifacts only. SQLite is the runtime state.

Primary command contract:

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

Creates offline artifacts from SQLite. Export does not power the live report.

Backward compatibility with the old command behavior is not required because the project is still early.

## 2. Goals

- Use SQLite as the only live reporting store.
- Use Textual for terminal UI and browser-served live report.
- Stop loading report JSON files into memory for live dashboards.
- Preserve raw OpenTelemetry attributes for reprocessing.
- Promote stable fields into canonical typed tables.
- Support memory attributes from OpenTelemetry hooks.
- Support specs, requirements, evidence, memory, privacy, cost, tokens, tools, MCP, models, repos, files, and graph relationships.
- Keep privacy and redaction enforced before data is stored or exported.
- Keep all reporting data queryable from SQLite.
- Keep schema evolution cheap by landing unknown attributes in `raw_events.attrs_json` first.

## 3. Non-goals

- No Kuzu.
- No Neo4j.
- No external graph DB by default.
- No hosted backend.
- No full prompt / response / tool payload storage by default.
- No live dashboard backed by JSON artifacts.
- No in-memory full report object as the runtime UI data model.

## 4. Storage layers

The database has five layers:

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
   Migrations, ingest sources, normalization runs, semantic convention versions.
```

Expansion rule:

```text
new telemetry field
  -> raw_events.attrs_json
  -> normalize if useful
  -> promote to Pydantic model field
  -> add nullable DB column through migration
  -> backfill from raw_events
  -> add index only when query patterns require it
  -> expose in Textual view model
```

## 5. Pydantic design

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
src/reflect/views/activity.py
src/reflect/views/sessions.py
src/reflect/views/session_detail.py
src/reflect/views/agents.py
src/reflect/views/models.py
src/reflect/views/tools.py
src/reflect/views/mcp.py
src/reflect/views/costs.py
src/reflect/views/graphs.py
src/reflect/views/specs.py
src/reflect/views/memory.py
src/reflect/views/privacy.py

src/reflect/tui/app.py
src/reflect/tui/screens/
src/reflect/tui/widgets/
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

## 6. SQLite defaults

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

## 7. Raw event store

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

Pydantic model:

```python
class RawEvent(ReflectModel):
    id: str
    source_id: str
    source_type: Literal["otlp_span", "otlp_log", "hook", "legacy_import"]
    event_type: str
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    session_id: str | None = None
    observed_at: datetime
    received_at: datetime
    attrs: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    normalized_status: Literal["pending", "ok", "failed", "ignored"] = "pending"
    normalization_error: str | None = None
    content_hash: str
    created_at: datetime
```

## 8. Canonical schema

### 8.1 agents, repos, files

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

### 8.2 sessions

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

Pydantic model:

```python
class SessionRecord(ReflectModel):
    id: str
    agent_id: str | None = None
    repo_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    status: Literal["running", "completed", "failed", "unknown"] = "unknown"
    title: str | None = None
    quality_score: float | None = Field(default=None, ge=0, le=100)
    failure_count: int = Field(default=0, ge=0)
    recovered_failure_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_creation_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0, ge=0)
    privacy_mode: Literal["metadata_only", "redacted_preview", "full_local", "full_encrypted"] = "metadata_only"
    source_kind: str | None = None
    source_ref: str | None = None
    created_at: datetime
    updated_at: datetime
```

### 8.3 steps

Every session is represented as ordered steps.

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

Pydantic model:

```python
class StepRecord(ReflectModel):
    id: str
    session_id: str
    parent_step_id: str | None = None
    seq: int = Field(ge=0)
    type: Literal[
        "llm_call", "tool_call", "mcp_call", "shell_command",
        "file_read", "file_write", "memory_event", "spec_event",
        "error", "unknown",
    ]
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    status: Literal["ok", "error", "skipped", "unknown"] = "unknown"
    summary: str | None = None
    raw_attrs: dict[str, Any] = Field(default_factory=dict)
```

## 9. LLM, tools, MCP

### 9.1 llm_calls

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

### 9.2 tool_calls

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

### 9.3 mcp_calls

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

## 10. Specs, requirements, evidence

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

Pydantic requirement model:

```python
class RequirementRecord(ReflectModel):
    id: str
    spec_id: str
    title: str
    description: str | None = None
    status: Literal["planned", "in_progress", "partial", "implemented", "validated", "blocked", "dropped"] = "planned"
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    evidence_status: Literal["missing", "partial", "present", "validated"] = "missing"
    confidence: float = Field(default=0, ge=0, le=1)
    created_at: datetime
    updated_at: datetime
```

## 11. Memory

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

Pydantic memory model:

```python
class MemoryRecord(ReflectModel):
    id: str
    scope: Literal["global", "workspace", "repo", "directory", "file", "spec", "agent", "user", "team"]
    type: Literal[
        "repo_convention", "validation_command", "test_command", "build_command",
        "known_failure", "successful_fix", "architecture_note", "style_preference",
        "unsafe_pattern", "tool_preference", "spec_decision", "deployment_rule",
        "ownership_hint", "unknown",
    ]
    repo_id: str | None = None
    file_id: str | None = None
    session_id: str | None = None
    step_id: str | None = None
    spec_id: str | None = None
    content_hash: str | None = None
    content_preview_redacted: str | None = Field(default=None, max_length=512)
    confidence: float = Field(default=0.5, ge=0, le=1)
    sensitivity: Literal["low", "medium", "high", "secret", "unknown"] = "unknown"
    source: Literal["opentelemetry_hook", "derived", "manual", "legacy_import"]
    expires_at: datetime | None = None
    last_seen_at: datetime | None = None
    raw_attrs: dict[str, Any] = Field(default_factory=dict)
```

Supported `gen_ai.memory.*` attributes:

```text
gen_ai.memory.id
gen_ai.memory.scope
gen_ai.memory.type
gen_ai.memory.content_hash
gen_ai.memory.content_preview
gen_ai.memory.confidence
gen_ai.memory.sensitivity
gen_ai.memory.source
gen_ai.memory.evidence.session_id
gen_ai.memory.evidence.step_id
gen_ai.memory.evidence.file_path
gen_ai.memory.evidence.repo
gen_ai.memory.evidence.command
gen_ai.memory.expires_at
gen_ai.memory.last_seen_at
```

## 12. Privacy and redaction

Storage enforcement comes before UI rendering.

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

## 13. Graph tables

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

Pydantic graph models:

```python
class GraphNodeRecord(ReflectModel):
    id: str
    type: Literal[
        "Session", "Step", "Agent", "Model", "Tool", "MCPServer", "Repo", "File",
        "Command", "Error", "Spec", "Requirement", "Evidence", "Memory",
        "PrivacyFinding", "CostBucket", "TokenBucket",
    ]
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class GraphEdgeRecord(ReflectModel):
    id: str
    from_node_id: str
    to_node_id: str
    type: Literal[
        "HAS_STEP", "USED_AGENT", "USED_MODEL", "CALLED_TOOL", "CALLED_MCP_SERVER",
        "IN_REPO", "TOUCHED_FILE", "READ_FILE", "WROTE_FILE", "RAN_COMMAND",
        "FAILED_WITH", "HAS_COST", "HAS_TOKENS", "WORKED_ON_SPEC",
        "IMPLEMENTS_REQUIREMENT", "VALIDATED_BY", "SUPPORTED_BY", "HAS_MEMORY",
        "MENTIONS_FILE", "MENTIONS_COMMAND", "HAS_PRIVACY_FINDING", "FOLLOWED_BY",
        "CO_OCCURRED_WITH",
    ]
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
```

## 14. Rollups

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

## 15. Metadata tables

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

## 16. Textual UI

Required command behavior:

```bash
reflect
```

Starts terminal Textual UI.

```bash
reflect report
```

Starts browser-served Textual UI.

```bash
reflect export --format html --output report.html
reflect export --format json --output report.json
```

Creates static artifacts from SQLite.

```bash
reflect db migrate
reflect db rebuild
reflect db rebuild-rollups
reflect db vacuum
reflect doctor
```

DB maintenance and validation.

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

### 16.1 Screen behavior

Overview:

```text
Show session count, agent count, model count, token totals, estimated cost, failure count, recovered failures, top sessions, top models, top tools.
Read from rollups first, then canonical tables for drilldown.
```

Activity:

```text
Show daily and hourly activity from daily_rollups and sessions.
Do not scan raw_events.
```

Sessions:

```text
Paginated DataTable of sessions.
Filter by agent, repo, model, status, date range, cost range, failure count.
Selecting a row opens Session Detail.
```

Session Detail:

```text
Timeline from steps.
LLM calls from llm_calls.
Tools from tool_calls.
MCP from mcp_calls.
Files from graph edges.
Privacy from privacy_findings.
Memory and specs from graph edges and canonical tables.
```

Agents:

```text
Compare sessions, tokens, cost, failures, models, tools, MCP usage by agent.
```

Models:

```text
Compare calls, tokens, cache usage, reasoning tokens, cost, latency, agents, sessions.
```

Tools:

```text
Show call count, error count, error rate, average duration, p95 duration, top sessions, top agents.
Use tool_rollups where possible.
```

MCP:

```text
Show server names, transport, protocol version, tool names, call count, error rate, latency.
```

Costs:

```text
Show cost by session, model, agent, repo, day.
Separate input, output, cache creation, cache read, and reasoning token costs when available.
```

Graphs:

```text
Show local graph neighborhoods from graph_nodes and graph_edges.
Start with tables and tree views, not D3 parity.
```

Observations:

```text
Show generated insights backed by SQL queries and evidence links.
No insight should require loading JSON report artifacts.
```

Specs:

```text
Show specs, requirements, status, evidence, drift, missing validation, and related sessions/files.
```

Memory:

```text
Show memories by scope, type, repo, file, spec, confidence, last seen, expiration, sensitivity.
Support low-confidence and stale memory filters.
```

Privacy:

```text
Show capture mode, redaction findings, sensitive fields, export safety, and blocked raw fields.
```

Exports:

```text
Show export options and confirm whether raw content is excluded.
```

## 17. View models

Suggested view model files:

```text
src/reflect/views/overview.py
src/reflect/views/activity.py
src/reflect/views/sessions.py
src/reflect/views/session_detail.py
src/reflect/views/agents.py
src/reflect/views/models.py
src/reflect/views/tools.py
src/reflect/views/mcp.py
src/reflect/views/costs.py
src/reflect/views/graphs.py
src/reflect/views/specs.py
src/reflect/views/memory.py
src/reflect/views/privacy.py
```

Example model:

```python
class OverviewViewModel(ReflectModel):
    session_count: int
    agent_count: int
    model_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    failure_count: int
    recovered_failure_count: int
    top_sessions: list[dict]
    top_models: list[dict]
    top_tools: list[dict]
```

View model rule:

```text
View models may aggregate SQL results.
View models must not load report JSON files.
View models must be small enough for screen rendering.
Large tables must be paginated.
Long queries must run through Textual workers.
```

## 18. Static HTML export

Static HTML remains useful, but it is not the live dashboard.

```bash
reflect export --format html --output report.html
```

The static HTML export must query SQLite directly. It may serialize a report artifact internally as part of the export process, but this artifact must not become the live reporting runtime.

Static export tabs should preserve parity with the existing demo experience:

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
Specs
Memory
Privacy
Exports
```

Default static export must exclude raw prompts, responses, tool inputs, tool outputs, and file contents.

## 19. Migration plan

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

Normalization must be idempotent. It must be safe to rerun.

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

## 20. Codex implementation guidance

Implement in small PRs. Do not try to port the full UI and full schema in one change.

Recommended PR order:

```text
PR 1. Add Pydantic schema package and model tests.
PR 2. Add SQLite store, migrations, DB connection wrapper, doctor command.
PR 3. Add raw_events ingestion and dedupe.
PR 4. Add normalization into sessions, steps, llm_calls, tool_calls, mcp_calls.
PR 5. Add graph_nodes, graph_edges, memories, privacy_findings.
PR 6. Add rollups and rebuild command.
PR 7. Add Textual shell and Overview screen from SQLite.
PR 8. Add Sessions and Session Detail screens.
PR 9. Add Agents, Models, Tools, MCP, Costs screens.
PR 10. Add Specs, Memory, Privacy screens.
PR 11. Change `reflect` and `reflect report` command behavior.
PR 12. Add static export from SQLite and remove live JSON dashboard path.
```

Quality rules for Codex:

```text
Prefer explicit migrations over implicit table creation.
Add tests for every migration.
Add tests for every Pydantic model that maps to a DB table.
Do not read dashboard JSON in Textual code.
Do not load all sessions into memory for paginated screens.
Do not store raw prompts, responses, tool inputs, or tool outputs by default.
Keep imports lazy where they improve CLI startup time.
Use deterministic IDs for graph nodes and graph edges so rebuilds are idempotent.
Use SQL queries and rollups for reporting, not Python loops over all rows.
```

## 21. Success criteria

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

## 22. Final implementation rule

```text
JSON files are input or output artifacts only.
SQLite is the runtime state.
Pydantic is the logical contract.
SQL migrations are the physical contract.
Textual is the live UI.
```

This keeps Reflect local-first, expandable, testable, and not locked into the current dashboard artifact shape.
