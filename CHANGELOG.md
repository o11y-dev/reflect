# Changelog

## 0.4.3 (unreleased)

### Fixed
- Corrected license references from MIT to Apache 2.0 on the hosted pages (`SoftwareApplication` JSON-LD in `docs/index.html` and `docs/showcase.html`, plus showcase footer copy)
- Sessions tab now keeps locally discovered sessions visible even when no OTLP telemetry exists, marks which sessions have OTLP telemetry available, and adds an Observations detail tab alongside Conversation and Telemetry

### Changed
- Landing page (`docs/showcase.html`) rewritten to align with the o11y.dev mission and agent-agnostic positioning: headline now reads "Observability for any AI coding agent", supported agents (Antigravity, Claude Code, Copilot, Cursor, Gemini CLI, OpenCode) rendered as an alphabetical chip row, and privacy/demo framing demoted from hero to a secondary callout
- Landing page gains a "One platform, three surfaces" section covering `reflect`, `opentelemetry-hooks`, and the `Policies` engine (marked `v0.12 preview`), plus dual install paths (`pipx install o11y-reflect` and `pipx install opentelemetry-hooks`)
- Hosted dashboard (`docs/index.html`) `<title>` rewritten to lead with "AI observability", with a full meta description added

### Added
- SEO and social-sharing metadata on both `docs/index.html` and `docs/showcase.html`: meta description, canonical URL, Open Graph tags, Twitter card tags, and `SoftwareApplication` + `Organization` JSON-LD
- `docs/robots.txt`, `docs/sitemap.xml`, and `docs/favicon.svg`
- `pages-check` CI job now asserts the new SEO metadata, required static files, and an OpenTelemetry mention on both pages so the positioning can't silently regress

## 0.4.2 (2026-04-16)

### Fixed
- `reflect skills` interactive picker no longer produces a staircase layout — in raw terminal mode `\n` is now replaced with `\r\n` so each item starts at column 0

### Added
- `cursor-agent`, `copilot`, and `opencode` added to `_SKILL_AGENT_SPECS` so they appear in the "which CLI should extract skills?" picker when installed
- `opencode` added to `_AGENT_SPECS` (detected via `~/.config/opencode`) and `_IMPLEMENTED_AGENT_SUPPORT` so it receives skills at install time and shows up in `reflect agents`

## 0.4.1 (2026-04-15)

### Fixed
- `reflect setup` Step 4 now writes `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_PROTOCOL` into `otel_config.json` via `setdefault`, so the gateway address lives in the config file rather than only in hardcoded fallback defaults across five native-OTel configurers
- `reflect doctor` / `_detect_hook_drift` flags a missing or unsupported `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_PROTOCOL` as drift, prompting existing installations to re-run `reflect setup`

### Changed
- Centralized OTLP endpoint/protocol config key names and defaults into `_HOOK_CFG_*` constants — a key rename or default change is now a one-line edit
- Pinned `opentelemetry-hooks==0.11.0` in all pipx install calls via `_HOOK_PACKAGE_SPEC` constant for reproducible installs

## 0.4.0 (2026-04-15)

### Added
- Local OTLP gateway (`reflect gateway`) that accepts traces and logs over gRPC (:4317) and HTTP (:4318) and writes them to `~/.reflect/state/otlp/`
- `reflect gateway start/stop/status` daemon management commands
- `reflect setup` now auto-starts the gateway after configuring native OTel
- `reflect doctor` shows gateway running/stopped status in Overview
- `reflect skills` session serialization now encodes trace-derived workflow fingerprints: ordered `tool_flow` (consecutive repeats collapsed), `shell_cmds`, prompt topic snippets (first 80 chars, whitespace-normalized), and `error_recovery` chains — giving the extraction AI real behavioral evidence instead of bare tool lists
- `reflect skills` interactive skill selection: space-to-toggle checkboxes with ↑↓ navigation and Enter to confirm; falls back to numbered-list prompt when stdin is not a TTY or raw-terminal mode is unavailable (Windows)
- `reflect skills` interactive agent selection: when multiple agent CLIs are installed, prompts with an arrow-key radio picker instead of silently picking the first detected one

### Fixed
- `_extract_recovery_chains` now sorts spans by timestamp before pairing and skips non-actionable event types (Stop, SessionEnd, etc.) so failure→recovery pairs reflect actual workflows
- `_serialize_sessions_for_skills` sorts `tool_seq` by timestamp before compressing so `tool_flow` is chronologically accurate
- `_interactive_pick` guards `tty`/`termios` imports behind `try/except ImportError` for Windows compatibility

### Dependencies
- Added `grpcio>=1.60` and `opentelemetry-proto>=1.20`
- Added `httpx>=0.24` to test dependencies (required by FastAPI TestClient)

## 0.3.0 (2026-04-14)

### Added
- Rich loading spinner with live status feedback during agent subprocess in `reflect skills`
- Fallback changelog stamping in `bump_version.py` when unreleased section version differs from target

### Fixed
- JSON fence parsing in skills command strips markdown code fences from agent output before parsing

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
