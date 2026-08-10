# AGENTS.md — reflect

Guidance for AI agents working in this repository.

## What this project is

**reflect** is a local-first CLI for AI agent telemetry and measurable workflow improvement. It reads OTLP plus supported agent-native stores, normalizes them into one SQLite model, and renders CLI, MCP, and browser views. Current client capabilities come from `agent_capabilities.py`; Gemini CLI is historical ingestion, while Antigravity is the current partial MCP/headless target.

CLI entry point: `reflect.core:main`
Installed as: `reflect` for releases via `pipx install .`; source development uses Poetry.

## Running it

```bash
# Install dependencies for source-based development
poetry install --extras test

# Open the local browser dashboard
poetry run reflect --otlp-traces ~/.reflect/state/otlp/otel-traces.active.jsonl

# Demo and health checks
poetry run reflect --demo
poetry run reflect doctor
```

## Key files

| File | Purpose |
|------|---------|
| `src/reflect/core.py` | CLI entrypoint and high-level orchestration |
| `src/reflect/parsing.py` | Finds local telemetry sources and OTLP inputs |
| `src/reflect/processing.py` | Span/session normalization and aggregation helpers |
| `src/reflect/telemetry_facts.py` | Shared pure skill/subagent evidence extraction for normalization and presentation |
| `src/reflect/models.py` | `TelemetryStats` and `AgentStats` dataclasses |
| `src/reflect/gateway.py` | Local OTLP gateway (gRPC + HTTP servers, file writer, daemon lifecycle) |
| `src/reflect/preparation.py` | Snapshot lifecycle, policies, progress, and coordinator |
| `src/reflect/preparation_pipeline.py` | Explicit ingest/normalize/derive preparation pipeline |
| `src/reflect/cli/progress.py` | Shared Rich progress for snapshot preparation commands |
| `src/reflect/opencode_store.py` | Typed read-only access to OpenCode's native SQLite store |
| `src/reflect/conversation_adapters.py` | Provider-native session projection into dashboard conversations |
| `src/reflect/store/cursor_usage.py` | Provenance-marked Cursor transcript usage estimation and repair |
| `src/reflect/dashboard_query_common.py` | Shared typed query helpers and session quality read models |
| `src/reflect/dashboard_queries.py` | Shared bounded overview read models |
| `src/reflect/dashboard_sessions.py` | Session payload and drill-down queries |
| `src/reflect/dashboard_explore.py` | Explore view routing and bounded tab queries |
| `src/reflect/dashboard_improvements.py` | Findings, workflows, skills, and impact dashboard adapter |
| `src/reflect/dashboard_server.py` | Thin browser routes, cache, and publish server |
| `src/reflect/frontend/` | Canonical authored browser template, CSS, and JavaScript |
| `src/reflect/data/index.html` | Generated packaged browser artifact; do not edit directly |
| `src/reflect/graph.py` | Weekly activity trend derivation shared by SQL views |
| `src/reflect/insights/` | Session-quality scoring and its local distribution profile |
| `docs/` | Hosted docs and dashboard artifacts |
| `src/reflect/data/skills/reflect/` | Canonical tracked and packaged `reflect` skill |
| `tests/` | Fast regression coverage for parsing, CLI, dashboard JSON, graphs, raw segments, and skill packaging |

## Architecture in one paragraph

`parsing.py` discovers source inputs, `store/ingest.py` checkpoints them, and `store/normalize.py` promotes them into canonical SQLite evidence. `processing.py` and `analyze_telemetry()` build `TelemetryStats` for bounded agent-assisted analysis. The browser queries the same SQLite store through `dashboard_queries.py`; `dashboard_server.py` only wires routes and lifecycle. Native conversation adapters provide high-fidelity session detail but never feed aggregates. Workflow evidence and impact use execution units, while sessions remain navigation and aggregate-usage containers. Never aggregate from already-shaped session cards.

## Conventions

- **There is a real test suite.** Start with the narrowest relevant tests in `tests/`, then run the full suite for broad changes.
- **Module split is intentional now.** Do not collapse code back into `core.py`.
- **Preserve the canonical data flow.** `TelemetryStats` is the source of truth. Dashboard/session summary rows are presentation data, not aggregation inputs.
- **Fallback gracefully.** Many attributes are optional, especially for non-OTLP local session sources. Guard optional fields explicitly.
- **Keep optional dependency behavior intact.** `dashboard_server.py` imports FastAPI inside the publish server path on purpose.
- **Use one-way migrations, not runtime compatibility layers.** Move durable data in a numbered migration, then delete old readers, dual writes, routes, aliases, and fallback branches.
- **Treat generated browser files as build outputs.** Edit `src/reflect/frontend/`, run `poetry run python scripts/build_dashboard.py`, and verify `src/reflect/data/index.html` and `docs/report.html` remain byte-identical.
- **orjson first, stdlib json fallback.** Reuse the existing import shim pattern.
- **Keep the changelog release-ready.** If your work adds features, fixes bugs, or changes dependencies, add or update a `## 0.x.x (unreleased)` section at the top of `CHANGELOG.md` before finishing. The release automation (`scripts/bump_version.py`) matches that exact heading pattern and stamps it with the version and date on release. Group entries under `### Added`, `### Fixed`, `### Changed`, or `### Dependencies` as appropriate. Do **not** use `## Unreleased` — it will not be picked up by the release script.
- **If you test the pipx-installed live dashboard, source edits are not enough.** Sync changed files into `~/.local/pipx/venvs/o11y-reflect/lib/python*/site-packages/reflect/` or reinstall before validating `reflect`.
- **Roadmap items do not live here by default.** If you discover durable roadmap or future-work items while working in `reflect`, mirror them into `../office/roadmap.md` or `../office/plan.md`. Keep this repo focused on implementation guidance and repo-local decisions.

## Engineering design defaults

- **Prefer OOP for new stateful or swappable behavior.** When a change introduces lifecycle, configuration, strategy selection, adapters, stores, or renderer-like behavior, default to small classes with explicit methods instead of growing procedural branches. Keep pure functions for stateless transformations.
- **Write agnostic, interchangeable code.** Avoid hard-coding one agent, vendor, transport, storage backend, or renderer into shared logic. Put provider-specific behavior behind narrow adapters or strategy objects so Claude, Codex, Copilot, Cursor, Gemini, hooks, native OTLP, JSONL, SQLite, CLI, and dashboard paths can evolve independently.
- **Keep contracts explicit.** Prefer typed dataclasses/models, protocols, and small interface surfaces over loosely shaped dict plumbing across module boundaries. If dicts are the existing contract, normalize them at the boundary and document required keys in tests.
- **Keep code and architecture lean.** Make the smallest coherent change that preserves the architecture. Every new class, module, helper, or interface must own meaningful state or lifecycle, remove demonstrated duplication, or isolate a real variant. Prefer reusing existing domain objects and consolidating paths over parallel abstractions. During review, remove redundant functions, wrappers, and fixtures, and compare complexity and code size before and after. Avoid speculative frameworks and unrelated refactors.
- **Composition over condition piles.** When branching grows around agent type, source type, or output target, introduce a mapper/adapter/strategy and register it close to the relevant domain instead of adding long `if/elif` ladders in orchestration code.

## Release cycle

- **Every releasable change keeps `CHANGELOG.md` current.** Add entries under the top `## 0.x.x (unreleased)` heading as work lands. Use `### Added`, `### Fixed`, `### Changed`, or `### Dependencies`.
- **Version and changelog move together.** For a proper release, `pyproject.toml` should already contain the target version and `CHANGELOG.md` should contain `## <version> (unreleased)` before the release workflow stamps the date.
- **Before saying a release is ready, check both remote and local state.** Report GitHub PR/check status separately from local validation and local uncommitted/untracked files. A mergeable PR with no reported checks is not the same as CI passing.
- **Local release gate:** run `poetry run ruff check .`, `poetry run pytest -q --no-cov`, and `poetry run python scripts/release_workflow.py release-notes <version>` before declaring readiness for a proper release.
- **Release after merge.** Treat PR merge to `main` as the handoff point for a proper release unless the user explicitly asks for a branch-based or emergency release.
- **Update path matters.** `reflect update --apply` upgrades both `o11y-reflect` and `opentelemetry-hooks` via pipx; keep that behavior and docs aligned so users get the latest hook package when updating reflect.

## Visual style guidelines

Use the current `docs/showcase.html` page as the product visual baseline for public pages and the browser dashboard.

- **Brand palette:** near-black `#050505`, signal orange `#F28A1A`, warm off-white `#F5F2EA`, muted warm text such as `#D7D1C6` / `#BEB8AD`, and graphite panels. Avoid reverting primary chrome to blue/purple gradients.
- **Logo:** use the clean product mark: off-white triangle with an orange ring/lens centered optically low, around 60% of mark height, on a near-black field. The dashboard header mark should match the showcase mark and link to `https://reflect.o11y.dev/`.
- **Surface language:** prefer sharp, technical, premium UI: 6-8px panel/card radii, restrained borders, warm shadows, dense information hierarchy, and orange used as signal/activity/insight.
- **Dashboard parity:** author changes in `src/reflect/frontend/`, then run `scripts/build_dashboard.py` so `src/reflect/data/index.html` and `docs/report.html` stay byte-for-byte identical. `docs/report.html` is what local `reflect` serves, not `docs/index.html` (the marketing page). If validating through pipx, reinstall the package before checking the live UI.
- **Compare/report emphasis:** active tabs, filters, compare cards, selection states, and key dashboard accents should visibly use orange; do not rely only on subtle token swaps that leave a tab visually neutral.
- **Copy tone:** lead with concrete workflow pain and evidence: failures, stalls, limits, loops, token/cost burn, and better future human + AI runs.

## Validation commands

```bash
# Fast targeted validation
poetry run pytest tests/test_dashboard_sql_api.py -q

# Full suite
poetry run pytest -q

# Cheap syntax check for dashboard server code
poetry run python -m py_compile src/reflect/dashboard_*.py
```

## Data flow for new metrics

To add a new tracked metric:

1. Add field(s) to `TelemetryStats` in `src/reflect/models.py`
2. Thread the data through parsing / processing helpers
3. Populate the field during telemetry analysis
4. Export it in the renderer that needs it:
   - the owning `dashboard_*.py` query or adapter module
5. Add or update regression coverage in `tests/`

## High-value pitfalls

- **Do not rebuild filtered dashboard aggregates from `sessions[]` rows.** Those rows intentionally contain truncated top-N summaries for display.
- **Keep SQL graph queries bounded before joins.** High-volume agents such as Cursor can have tens of thousands of tool calls in one filtered report. Co-occurrence and dependency queries must filter to the displayed top tools and/or distinct `(session_id, tool_name)` pairs before self-joins; never self-join the full `tool_calls` table and trim afterward.
- **Cap per-session graph payloads.** Timeline-style widgets should limit spans per selected/heavy session, currently `500` spans per session in the SQL dashboard path. If a graph needs more detail, add pagination or drill-down rather than returning unbounded arrays from `/api/data`.
- **Treat `/api/data` as an interactive endpoint.** Filtered dashboard payloads should return in a few seconds on a large local SQLite store. If a new SQL widget needs expensive analysis, scope it by filtered `session_ids`, use rollup tables where possible, and validate with heavy filters such as `agents=cursor`.
- **Be careful with `from __future__ import annotations` in `dashboard_server.py`.** FastAPI route annotations must resolve in module globals when the inline publish server is created.
- **Keep browser state stable when touching filters.** URL filters, current tab, selected session, and comparison selection should survive server-backed dashboard refreshes when possible.

## Memory initiative takeaways

- **Current hook telemetry is already useful for memory.** In local OTLP traces, the strongest stable raw signals are `gen_ai.client.file_path` on file events and `gen_ai.client.cwd` on shell events. Use them before trying to infer work only from prompts or tool names.
- **Do not assume repo identity is already present.** The current traces do not reliably carry `repo.name`, `vcs.repository.name`, or `code.workspace.root`. If memory needs repo-aware grouping, derive it explicitly from `cwd` and file paths instead of waiting for those attributes to appear.
- **Prefer repo-relative normalization over absolute-path memory keys.** Absolute paths are valuable raw evidence, but they fragment memory across machines, editor cache directories, and home paths. Normalize file paths against the inferred repo/workspace root before storing durable memory facts.
- **`reflect setup` wires local/private telemetry first, with optional text capture.** It configures local OTLP export and runs `otel-hook setup --global`; hook config, spans, logs, and SQLite report data stay on the user's machine. Prompt/response text remains opt-in via setup capture mode, and setup still does not guarantee repo-scoped Copilot hook wiring or richer repo metadata on spans.
- **Be careful about source vs installed hook behavior.** The `opentelemetry-hooks/` source tree contains newer memory-summary code than the currently installed pipx package in this environment. When validating memory behavior, confirm which hook build is actually running.

## Up next

- **Frontend filtered loading cleanup** — backend filtered payload exactness is fixed; the remaining cleanup is simplifying the reload-oriented browser flow in `src/reflect/data/index.html`.
