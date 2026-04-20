# Changelog

## 0.6.0 (unreleased)

### Changed
- Deprecated `python serve.py` and legacy `reflect --publish` references in docs/UI copy; use `reflect report` (or `python3 -m reflect.core report`) to open the local dashboard.

## 0.6.0 (2026-04-20)

### Added
- **Distribution-aware insights engine** — observations, recommendations, strengths, and examples now use IQR-based outlier detection against the user's own baseline instead of hardcoded magic-number thresholds
- **Per-session insights** — each session is compared against the user's distribution (token usage, failure rate, duration, tool loops, cache utilization) and assigned structured `Insight` objects with severity, confidence, and evidence
- **Structured `Insight` type** with kind, title, body, category, severity (LOW/MEDIUM/HIGH/CRITICAL), confidence (0.0–1.0), and evidence dict — replaces raw strings internally while preserving backward-compatible API
- **`DataProfile`** — statistical summary computed once per analysis run, caching per-session distributions (tokens, tools, prompts, failures, duration) for adaptive thresholds
- **Cold-start fallback** — sparse data (< 5 sessions) falls back to conservative absolute thresholds instead of producing noisy results
- Session insights rendered in dashboard session cards and session detail API
- Achievement badge "High Leverage" now uses distribution-aware threshold (p95) instead of arbitrary 10:1
- `reflect doctor` now distinguishes missing, incomplete, unreadable, and ready native OTel agent configs for Claude Code, Copilot, Gemini CLI, and OpenAI Codex CLI

### Changed
- `src/reflect/insights.py` refactored into `src/reflect/insights/` package with modular signals, types, profile, scoring, and renderers
- Session quality scoring now uses 8-factor weighted model (completion, efficiency, reliability, loops, duration, recovery, diversity, productivity) with distribution-aware thresholds
- Signals only fire when data warrants it — no more always-fire noise for balanced/healthy usage
- Prompt examples are now domain-agnostic (removed hardcoded GitLab, Coralogix, ISR- references)
- `buildSessionObservations()` in the frontend now consumes backend-computed insights when available

### Fixed
- Balanced usage data no longer produces spurious "Context gathering looks controlled" observations
- `reflect setup` now writes explicit trace and log exporter settings into the OpenAI Codex CLI `[otel]` block
- OpenAI Codex CLI native OTel updates now preserve unrelated TOML sections when refreshing the `[otel]` block
- OpenAI Codex CLI native OTel setup now keeps prompt logging disabled by default
- Increased the default hosted/local dashboard font sizing baseline so tabs, metadata, and supporting UI text render at more readable sizes.

### Changed
- Native OTel config generation now derives agent-specific desired settings from shared local endpoint/protocol helpers instead of hand-rolling each agent writer
- README native-telemetry docs now spell out the exact config surfaces and privacy-sensitive defaults
- README now also documents that the local `reflect` gateway persists traces and logs, but not OTLP metrics

## 0.5.0 (2026-04-18)

### Fixed
- Corrected license references from MIT to Apache 2.0 on the hosted pages (`SoftwareApplication` JSON-LD in `docs/index.html` and `docs/showcase.html`, plus showcase footer copy)

### Changed
- Landing page (`docs/showcase.html`) rewritten to align with the o11y.dev mission and agent-agnostic positioning: headline now reads "Observability for any AI coding agent", supported agents (Antigravity, Claude Code, Copilot, Cursor, Gemini CLI, OpenCode) rendered as an alphabetical chip row, and privacy/demo framing demoted from hero to a secondary callout
- Landing page gains a "One platform, three surfaces" section covering `reflect`, `opentelemetry-hooks`, and the `Policies` engine (marked `v0.12 preview`), plus dual install paths (`pipx install o11y-reflect` and `pipx install opentelemetry-hooks`)
- Hosted dashboard (`docs/index.html`) `<title>` rewritten to lead with "AI observability", with a full meta description added
- Sessions tab now keeps locally discovered sessions visible even when no OTLP telemetry exists, marks which sessions have OTLP telemetry available, and adds an Observations detail tab alongside Conversation and Telemetry

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
