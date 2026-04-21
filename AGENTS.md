# AGENTS.md — reflect

Guidance for AI agents working in this repository.

## What this project is

**reflect** is a local-first CLI for AI agent telemetry. It reads OTLP traces plus local session stores for Claude, Copilot, Gemini, and Cursor, then renders terminal views, markdown reports, dashboard JSON, and the hosted publish view.

CLI entry point: `reflect.core:main`
Installed as: `reflect` (via `pipx install .` or `pip install -e .`)

## Running it

```bash
# Install editable package for source-based development
pip install -e .[test]

# Terminal dashboard (default)
python3 -m reflect.core --otlp-traces ~/.reflect/state/otlp/otel-traces.json

# Markdown report
python3 -m reflect.core --otlp-traces ~/.reflect/state/otlp/otel-traces.json --no-terminal --output reports/my-report.md

# Open local dashboard server
python3 -m reflect.core report --otlp-traces ~/.reflect/state/otlp/otel-traces.json
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

## Validation commands

```bash
# Fast targeted validation
python3 -m pytest tests/test_dashboard_json.py -q

# Full suite
python3 -m pytest -q

# Cheap syntax check for dashboard server code
python3 -m py_compile src/reflect/dashboard.py
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
- **Be careful with `from __future__ import annotations` in `dashboard.py`.** FastAPI route annotations must resolve in module globals when the inline publish server is created.
- **Keep browser state stable when touching filters.** URL filters, current tab, selected session, and comparison selection should survive server-backed dashboard refreshes when possible.

## Up next

- **Frontend filtered loading cleanup** — backend filtered payload exactness is fixed; the remaining cleanup is simplifying the reload-oriented browser flow in `src/reflect/data/index.html`.
