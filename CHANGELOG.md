# Changelog

## 0.3.0 (2026-04-14)

## 0.2.1 (2026-04-14)

### Added
- Chat-style conversation UI in hosted dashboard (prompts/responses as bubbles, tool events as compact chips)
- Auto-load session detail on selection (no manual button click)
- Google Tag Manager analytics on all web assets
- `reflect update` command with package drift detection
- Ruff linting enforced in CI (lint job runs before test matrix)
- Coverage gate at 65% via pytest-cov
- `__all__` public API surface in `__init__.py`
- Compact stats bar replacing 4-card grid in conversation view

### Fixed
- Mutable default argument bug in `_process_span` (shared `Counter` across calls)
- Double `webbrowser.open` on `--publish` (removed redundant call in `core.py`)
- Ruff violations (E731, E701, B007, SIM108, F841, W293) across codebase
- Replaced `print(file=sys.stderr)` with stdlib `logging` in library code

### Changed
- PyPI publish workflow uses API token auth (`CD` environment) instead of OIDC trusted publishing
- Test workflow reusable via `workflow_call` trigger
- Playwright moved from `test` to separate `e2e` optional dependency group

## 0.2.0

### Added
- `--demo` flag for instant terminal dashboard with bundled sample data
- Apache-2.0 license
- GitHub Actions CI and PyPI publishing workflows
- Published to PyPI as `o11y-reflect`

### Changed
- Package renamed from `reflect` to `o11y-reflect` for PyPI uniqueness (CLI command stays `reflect`)
- README rewritten for launch: hero promise, 3-step quickstart, support matrix

## 0.1.0

### Added
- Local-first CLI for AI coding agent telemetry analysis
- Terminal dashboard with Rich (token usage, tool efficiency, activity heatmaps, MCP tracking)
- Markdown report generation
- JSON dashboard artifact for hosted dashboards
- `--publish` flag to open hosted dashboard at reflect.o11y.dev
- `reflect setup` command: installs opentelemetry-hooks, wires agent configs, distributes skills
- `reflect doctor` command: agent discovery, telemetry status, recommendations
- Multi-agent support: Claude Code, GitHub Copilot, Gemini CLI, Cursor
- OTLP JSON trace parsing with rich session/log fallback adapters
- Subagent delegation tracking
- MCP server availability gap detection
- Tool failure rate analysis
- Token economy analysis (input, output, cache hits)
