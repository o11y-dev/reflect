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
  - local hook spans JSONL can be ingested into `raw_events` with the same dedupe path
  - richer native/session-store ingestion adapters are still pending

- 🚧 **Phase 4 — normalization**: **Not started**
  - canonical target tables exist
  - no `raw_events -> canonical tables/memory/graph/privacy` normalization pipeline yet

- 🚧 **Phase 5 — rollups**: **Partially complete**
  - rollup tables/migrations exist
  - no rollup rebuild jobs yet

- 🚧 **Phase 6 — Port Textual UI to SQL**: **Not started**
  - current runtime still uses existing terminal/dashboard code path

- 🚧 **Phase 7 — Replace `reflect report` with browser-served Textual**: **Not started**

- 🚧 **Phase 8 — Static export from SQLite**: **Not started**

- 🚧 **Phase 9 — Remove JSON runtime dependency**: **Not started**

## Immediate next execution backlog

1. Add normalization pipeline from `raw_events` into canonical tables.
2. Add richer native/session-store ingestion adapters while preserving `source_id + content_hash` dedupe.
3. Add rollup rebuild jobs for `session_rollups`, `daily_rollups`, and `tool_rollups`.
4. Start SQL-backed view models for at least Overview/Sessions and wire to Textual migration plan.

## Definition of done reminder

Spec is only fulfilled when runtime/reporting no longer depend on loading dashboard JSON into memory, and Textual/report surfaces are SQL-backed for live use.
