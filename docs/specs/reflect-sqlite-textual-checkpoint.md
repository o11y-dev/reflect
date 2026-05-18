# Reflect SQLite/Textual Spec — Execution Checkpoint (2026-05-01)

This checkpoint tracks implementation status for `docs/specs/reflect-sqlite-textual-spec.md`.

## Current status summary

We **still have substantial work remaining** to fully execute the spec.

Completed foundations now exist for:

- SQLite connection defaults (`foreign_keys`, WAL, `synchronous`, checkpoint, busy timeout)
- initial SQL migration file (`001_initial.sql`) and migration runner
- rollup and graph migration files (`002_rollups.sql`, `003_graph.sql`)
- canonical table migration file (`004_canonical.sql`)
- `reflect db migrate` CLI entrypoint
- `reflect db doctor` migration/foreign-key/pragma health check
- initial Pydantic base/event models and schema export command
- SQL ingestion now supports OTLP traces plus inferred sibling OTLP logs, including Codex native log events normalized into hook-like spans
- SQL-backed Overview and Sessions view models for the future Textual/report runtime
- `reflect report` browser server exposes SQL-backed Overview/Sessions APIs from the configured SQLite store
- temporary `reflect report --sql-only` guard materializes the SQLite store and serves from SQLite without building legacy dashboard JSON
- SQL-only browser payload now populates shared dashboard widget data for activity, events, agents, models, tools, costs, MCP counts, and basic graph/timeline views
- SQL-only mode loads session detail from SQLite and fills quality, pricing, skills/subagents, MCP server, observations, examples, badges, and token-economy fields from SQLite-derived data
- SQL-backed tab view models now cover Activity, Agents, Models, Tools, MCP, Costs, Graphs, Specs, Memory, Privacy, and Exports, with the browser compatibility payload consuming those shared view models
- SQL session detail now preserves trace/span parent metadata for browser telemetry trees, and SQL tool widgets normalize Docker/NPX MCP launch commands before display
- SQL normalization now persists canonical `steps.parent_step_id` from raw span parent IDs, with session detail using the stored parent relation and falling back to raw span resolution only for older stores
- SQL tab view models now derive skills/subagents from canonical steps, including explicit prompt invocations and structured skill tool calls, so Tools and Agents share the same SQL source for skill/subagent widgets
- Browser report now exposes a Data tab that renders SQL-backed Specs, Memory, Privacy, and Export readiness directly from `sqlite.tabs.*`
- regression tests for SQLite runtime pragmas, migration idempotency, and Pydantic allow/forbid behavior

## Phase-by-phase checkpoint

- ✅ **Phase 1 — Add schema package**: **Partially complete**
  - base schema models are present
  - initial event model is present
  - schema export command exists
  - missing most logical models (`entities`, `llm`, `tools`, `mcp`, `specs`, `memory`, `privacy`, `graph`, `rollups`)

- ✅ **Phase 2 — Add SQLite store**: **Partially complete**
  - connection wrapper + pragmas present
  - migration runner present
  - rollup and graph migrations present
  - canonical table migration present
  - DB doctor checks present

- 🚧 **Phase 3 — raw_events ingestion**: **In progress**
  - table exists
  - `reflect ingest --otlp` now ingests OTLP traces JSON into `raw_events` with `source_id + content_hash` dedupe (`db ingest-otlp` kept as legacy alias)
  - SQL report preparation ingests inferred OTLP log files beside the selected trace file and reports per-source inserted/skipped counts
  - local hook spans JSONL can be ingested into `raw_events` with the same dedupe path
  - richer native/session-store ingestion adapters are still pending

- 🚧 **Phase 4 — normalization**: **Partially complete**
  - canonical target tables exist
  - `reflect db normalize` promotes pending raw events into sessions, steps, LLM/tool/MCP call rows, memories, and privacy findings
  - `reflect db rebuild-graph` populates graph nodes and edges from canonical sessions, steps, tools, MCP calls, and memories

- 🚧 **Phase 5 — rollups**: **Partially complete**
  - rollup tables/migrations exist
  - `reflect db rebuild-rollups` refreshes session, daily, and tool aggregate tables from canonical data

- 🚧 **Phase 6 — Port Textual UI to SQL**: **In progress**
  - SQL-backed view models exist for Overview and paginated Sessions
  - dedicated SQL-backed view models exist for Activity, Agents, Models, Tools, MCP, Costs, Graphs, Specs, Memory, Privacy, and Exports
  - browser report server exposes those view models via `/api/sql/overview`, `/api/sql/sessions`, and `/api/data.sqlite`
  - `reflect report --sql-only` materializes the SQLite store from selected/default OTLP traces before serving, then proves SQL-backed serving without legacy dashboard JSON
  - SQL-only mode supplies shared dashboard widget fields from SQLite for existing tabs, including session detail, costs, quality, MCP, skills/subagents, observations, examples, badges, token economy, specs, memory, privacy, and export readiness
  - session detail carries trace/span identifiers and resolved parent row IDs so corrected hook parent relationships can render in the browser timeline
  - canonical `steps.parent_step_id` is populated during normalization from `raw_events.span_id` / `parent_span_id`
  - Tools and Agents consume shared SQL-derived skill/subagent counts from tab view models instead of dashboard-only compatibility logic
  - Browser Data tab renders Specs, Memory, Privacy, and Exports from `sqlite.tabs.*`
  - current runtime still uses existing terminal/dashboard code path

- 🚧 **Phase 7 — Replace `reflect report` with browser-served Textual**: **Not started**

- 🚧 **Phase 8 — Static export from SQLite**: **Not started**

- 🚧 **Phase 9 — Remove JSON runtime dependency**: **Not started**

## Immediate next execution backlog

1. Expand `--sql-only` coverage surface-by-surface until every current browser tab renders directly from SQLite tab payloads instead of compatibility fields.
2. Implement static export from SQLite-backed view models.
3. Add richer native/session-store ingestion adapters as a postponed follow-up track.

## Definition of done reminder

Spec is only fulfilled when runtime/reporting no longer depend on loading dashboard JSON into memory, and Textual/report surfaces are SQL-backed for live use.
