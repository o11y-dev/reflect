# AGENTS.md — reflect

Guidance for AI agents working in this repository.

## What this project is

**reflect** is a local-first CLI for AI agent telemetry. It reads OTLP traces plus local session stores for Claude, Copilot, Gemini, and Cursor, then renders terminal views, markdown reports, dashboard JSON, and the hosted publish view.

CLI entry point: `reflect.core:main`
Installed as: `reflect` for releases via `pipx install .`; source development uses Poetry.

## Running it

```bash
# Install dependencies for source-based development
poetry install --extras test

# Terminal dashboard (default)
poetry run reflect --otlp-traces ~/.reflect/state/otlp/otel-traces.json

# Markdown report
poetry run reflect --otlp-traces ~/.reflect/state/otlp/otel-traces.json --no-terminal --output reports/my-report.md

# Open local dashboard server
poetry run reflect report --otlp-traces ~/.reflect/state/otlp/otel-traces.json

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
| `src/reflect/models.py` | `TelemetryStats` and `AgentStats` dataclasses |
| `src/reflect/gateway.py` | Local OTLP gateway (gRPC + HTTP servers, file writer, daemon lifecycle) |
| `src/reflect/dashboard.py` | Dashboard JSON, publish server, session detail APIs |
| `src/reflect/data/index.html` | Browser dashboard UI |
| `src/reflect/graph.py` | Tool transition, co-occurrence, latency, and timeline graph derivation |
| `src/reflect/insights.py` | Observations, recommendations, achievements, token economy |
| `src/reflect/report.py` | Markdown report rendering |
| `src/reflect/terminal.py` | Terminal dashboard rendering |
| `reports/` | Generated markdown reports |
| `docs/` | Hosted docs and dashboard artifacts |
| `skills/reflect/` | Canonical repo-root `reflect` skill package |
| `tests/` | Fast regression coverage for parsing, CLI, dashboard JSON, graphs, terminal output, and skill packaging |

## Architecture in one paragraph

`parsing.py` finds and normalizes raw inputs, `processing.py` and `analyze_telemetry()` build a canonical `TelemetryStats`, then renderers fan out from that shared state: `terminal.py` for the CLI dashboard, `report.py` for markdown, and `dashboard.py` plus `data/index.html` for the browser dashboard and local publish server. When fixing filtered dashboard behavior, prefer deriving from canonical per-session telemetry maps in `TelemetryStats`, not from already-shaped session cards.

## Conventions

- **There is a real test suite.** Start with the narrowest relevant tests in `tests/`, then run the full suite for broad changes.
- **Module split is intentional now.** Do not collapse code back into `core.py`.
- **Preserve the canonical data flow.** `TelemetryStats` is the source of truth. Dashboard/session summary rows are presentation data, not aggregation inputs.
- **Fallback gracefully.** Many attributes are optional, especially for non-OTLP local session sources. Guard optional fields explicitly.
- **Keep optional dependency behavior intact.** `dashboard.py` imports FastAPI inside the publish server path on purpose.
- **orjson first, stdlib json fallback.** Reuse the existing import shim pattern.
- **Keep the changelog release-ready.** If your work adds features, fixes bugs, or changes dependencies, add or update a `## 0.x.x (unreleased)` section at the top of `CHANGELOG.md` before finishing. The release automation (`scripts/bump_version.py`) matches that exact heading pattern and stamps it with the version and date on release. Group entries under `### Added`, `### Fixed`, `### Changed`, or `### Dependencies` as appropriate. Do **not** use `## Unreleased` — it will not be picked up by the release script.
- **If you test the pipx-installed live dashboard, source edits are not enough.** Sync changed files into `~/.local/pipx/venvs/o11y-reflect/lib/python*/site-packages/reflect/` or reinstall before validating `reflect report`.
- **Roadmap items do not live here by default.** If you discover durable roadmap or future-work items while working in `reflect`, mirror them into `../office/roadmap.md` or `../office/plan.md`. Keep this repo focused on implementation guidance and repo-local decisions.

## Visual style guidelines

Use the current `docs/showcase.html` page as the product visual baseline for public pages and the browser dashboard.

- **Brand palette:** near-black `#050505`, signal orange `#F28A1A`, warm off-white `#F5F2EA`, muted warm text such as `#D7D1C6` / `#BEB8AD`, and graphite panels. Avoid reverting primary chrome to blue/purple gradients.
- **Logo:** use the clean product mark: off-white triangle with an orange ring/lens centered optically low, around 60% of mark height, on a near-black field. The dashboard header mark should match the showcase mark and link to `https://reflect.o11y.dev/`.
- **Surface language:** prefer sharp, technical, premium UI: 6-8px panel/card radii, restrained borders, warm shadows, dense information hierarchy, and orange used as signal/activity/insight.
- **Dashboard parity:** keep `src/reflect/data/index.html` and `docs/index.html` in sync for browser dashboard changes. If validating through pipx, also sync the installed package copy before checking `reflect report`.
- **Compare/report emphasis:** active tabs, filters, compare cards, selection states, and key dashboard accents should visibly use orange; do not rely only on subtle token swaps that leave a tab visually neutral.
- **Copy tone:** lead with concrete workflow pain and evidence: failures, stalls, limits, loops, token/cost burn, and better future human + AI runs.

## Validation commands

```bash
# Fast targeted validation
poetry run pytest tests/test_dashboard_json.py -q

# Full suite
poetry run pytest -q

# Cheap syntax check for dashboard server code
poetry run python -m py_compile src/reflect/dashboard.py
```

## Data flow for new metrics

To add a new tracked metric:

1. Add field(s) to `TelemetryStats` in `src/reflect/models.py`
2. Thread the data through parsing / processing helpers
3. Populate the field during telemetry analysis
4. Export it in the renderer that needs it:
   - `terminal.py`
   - `report.py`
   - `dashboard.py`
5. Add or update regression coverage in `tests/`

## High-value pitfalls

- **Do not rebuild filtered dashboard aggregates from `sessions[]` rows.** Those rows intentionally contain truncated top-N summaries for display.
- **Keep SQL graph queries bounded before joins.** High-volume agents such as Cursor can have tens of thousands of tool calls in one filtered report. Co-occurrence and dependency queries must filter to the displayed top tools and/or distinct `(session_id, tool_name)` pairs before self-joins; never self-join the full `tool_calls` table and trim afterward.
- **Cap per-session graph payloads.** Timeline-style widgets should limit spans per selected/heavy session, currently `500` spans per session in the SQL dashboard path. If a graph needs more detail, add pagination or drill-down rather than returning unbounded arrays from `/api/data`.
- **Treat `/api/data` as an interactive endpoint.** Filtered dashboard payloads should return in a few seconds on a large local SQLite store. If a new SQL widget needs expensive analysis, scope it by filtered `session_ids`, use rollup tables where possible, and validate with heavy filters such as `agents=cursor`.
- **Be careful with `from __future__ import annotations` in `dashboard.py`.** FastAPI route annotations must resolve in module globals when the inline publish server is created.
- **Keep browser state stable when touching filters.** URL filters, current tab, selected session, and comparison selection should survive server-backed dashboard refreshes when possible.

## Memory initiative takeaways

- **Current hook telemetry is already useful for memory.** In local OTLP traces, the strongest stable raw signals are `gen_ai.client.file_path` on file events and `gen_ai.client.cwd` on shell events. Use them before trying to infer work only from prompts or tool names.
- **Do not assume repo identity is already present.** The current traces do not reliably carry `repo.name`, `vcs.repository.name`, or `code.workspace.root`. If memory needs repo-aware grouping, derive it explicitly from `cwd` and file paths instead of waiting for those attributes to appear.
- **Prefer repo-relative normalization over absolute-path memory keys.** Absolute paths are valuable raw evidence, but they fragment memory across machines, editor cache directories, and home paths. Normalize file paths against the inferred repo/workspace root before storing durable memory facts.
- **`reflect setup` wires local/private telemetry first, with optional text capture.** It configures local OTLP export and runs `otel-hook setup --global`; hook config, spans, logs, and SQLite report data stay on the user's machine. Prompt/response text remains opt-in via setup capture mode, and setup still does not guarantee repo-scoped Copilot hook wiring or richer repo metadata on spans.
- **Be careful about source vs installed hook behavior.** The `opentelemetry-hooks/` source tree contains newer memory-summary code than the currently installed pipx package in this environment. When validating memory behavior, confirm which hook build is actually running.

## Up next

- **Frontend filtered loading cleanup** — backend filtered payload exactness is fixed; the remaining cleanup is simplifying the reload-oriented browser flow in `src/reflect/data/index.html`.
