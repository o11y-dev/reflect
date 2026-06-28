# Changelog

## 0.8.5 (unreleased)

### Fixed
- Updated `reflect update --apply` to upgrade both `o11y-reflect` and `opentelemetry-hooks` via pipx.
- Added adapter-level transcript token estimates for native Cursor session ingest so SQL rollups no longer show zero tokens when exact Cursor usage is unavailable locally, without mutating raw span events.
- Fixed SQL Tools tab command-pattern extraction so hook event summaries such as `gen_ai.client.hook.PreToolUse` are not counted as shell commands, while `rtk` command patterns retain both CLI family and action.

## 0.8.4 (2026-06-16)

### Changed
- Renamed the bundled skills-extraction helper skill from `skills` to `reflect-skills` so generated/distributed skill naming is clearly reflect-scoped.
- Updated `reflect skills` to augment extraction prompts with SQL Behavioral Memory Graph evidence from the SQLite store (with telemetry-based fallback when graph evidence is unavailable).
- Migrated `reflect skills` session-stat evidence generation to SQL canonical tables by default, including both SQL stats and Behavioral Memory Graph evidence in extraction bundles.
- Updated `reflect setup` skill distribution to install `reflect-skills` alongside `reflect` and `opentelemetry-skill`.
- Added interactive agent selection before skill installation so `reflect skills` can target a subset of detected agents in terminals.
- Cleaned up legacy `skills/` aliases during skill distribution so renamed bundles do not leave stale directories behind.
- Expanded report ingest summaries with hook-event counts per source and agent, so hook-derived telemetry is visible alongside native OTLP and session-file inputs.
- Made bare `reflect` open the local browser report from SQLite by default, leaving `reflect report` as a deprecated compatibility alias and terminal/markdown/JSON outputs behind explicit deprecated flags.
- Updated public docs and bundled reflect skill guidance around the Behavioral Memory Graph and the new default browser-report workflow.
- Added a session-level `Tools` tab that persists tool, skill, MCP tool, MCP server, and subagent usage into the session detail view.
- Expanded skill detection so `SKILL.md` reads and prompt text hints are surfaced, not just explicit `skill` tool calls.
- Normalized subagent detection across Cursor, Claude, and Copilot-style events, including nested tool-input file paths.
- Added instruction file discovery and memory upsert into SQLite via `reflect db sync-instructions`.
- Added a Behavioral Memory Graph canvas to the report Graphs tab, backed by `graph_nodes` and `graph_edges`.
- Added per-session `Folder` graph nodes derived from touched paths, linking sessions and tool calls to the folders they investigated or edited.
- Added durable telemetry provenance on SQL `raw_events` and `steps`, separating transport/source origin from semantic event style in the browser overview.
- Added an Overview cost-trends chart that breaks estimated cost down over time by agent.

### Fixed
- Fixed `reflect skills --agent codex` to use the Codex CLI's non-interactive `exec` subcommand instead of the unsupported top-level `--print` flag.
- Fixed bundled package data so releases include the renamed `reflect-skills` helper skill instead of the removed legacy `skills` path.
- Made `reflect doctor cost` resilient to transient SQLite lock contention by retrying locked operations and increasing default SQLite busy timeout.
- Backfilled costs for Claude Code native OTLP log rows and Codex native session token-count rows during SQL report ingestion.
- Prevented duplicate model/token rows from overlapping local sources from inflating SQL session token and cost totals.
- Added the missing YAML frontmatter delimiter to the bundled `reflect-skills` skill.
- Fixed `content_preview_redacted` column in memories table to properly redact sensitive file paths from user/home directories (e.g., `~/.claude/CLAUDE.md`) by showing only path basename and metadata instead of full content.
- Fixed session detail `tool_inventory` to include `tool_result` events so tool durations and failures are properly captured for all telemetry sources (span, native, and conversation).
- Fixed OTLP ingest summaries so native Claude/Codex/Gemini OTLP log records are counted as native OTLP telemetry instead of hook telemetry after normalization.
- Fixed OTLP provenance repair so existing SQLite rows are backfilled without reingest, and excluded reflect-injected provenance markers from raw-event dedupe hashes.
- Fixed SQL rollups so session and daily error counts do not double-count failures already represented by canonical error steps.
- Fixed SQL context-tab classification so Cursor plan artifacts render under Specs instead of the Memory widget.
- Fixed the semantic/memory graph so specs render as first-class `Spec` nodes, including Cursor plan artifacts, instead of leaking through as generic memory nodes.
- Fixed semantic graph orphaning for instruction, memory, and plan nodes by pulling their connected session context into the rendered graph and inferring repo links for workspace-scoped instruction files.
- Fixed session-filtered semantic graph views so connected memory and instruction context stays visible through repo/path bridges instead of disappearing unless nodes had a direct session stamp.

## 0.8.2 (2026-05-26)

### Added
- Added `reflect doctor cost` to scan the SQLite store for observed model names, append only missing model aliases to `~/.reflect/config/model-aliases.json`, and refresh SQL cost estimates.

### Changed
- Run SQL normalization, cost-alias refresh, cost repricing, and rollup rebuilds after `reflect ingest` so newly ingested telemetry has cost data ready for reports.

## 0.8.2 (2026-05-26)

### Changed
- Make SQLite-backed report data the default `reflect report` runtime and keep `--sql-only` as a deprecated no-op compatibility flag.
- Restore the SQL checkpoint dashboard UI, including SQL-backed report panels, quality scoring, Codex native session ingestion, and report artifact generation from SQLite.

### Fixed
- Guard raw event normalization with savepoints so a failed event does not abort the full normalization batch.

## 0.8.1 (2026-05-26)

### Fixed
- Prepare the SQLite report store during the default `reflect report` path so SQL-backed dashboard data is populated without requiring `--sql-only`.

## 0.8.0 (2026-05-26)

### Added
- Added temporary `reflect report --sql-only` migration guard to materialize the SQLite store and serve browser report data from SQLite without building legacy dashboard JSON.
- Added SQL ingest support for inferred OTLP log files so Codex native log events are normalized into the SQLite report store alongside OTLP traces.
- Added Codex CLI native session-file ingestion from `~/.codex/sessions/**/*.jsonl` so local Codex prompts, assistant responses, and tool calls can populate the SQLite report store when OTLP logs are incomplete.
- Added SQL-only session detail loading plus SQL-derived report data for quality scores, costs, skills, subagents, MCP servers, observations, examples, badges, and token-economy widgets.
- Filled SQL-only cost widget breakdowns from SQLite LLM calls so input, output, and cache cost cards no longer render as zero when priced token data exists.
- Expanded the SQL-only browser payload to populate shared dashboard widgets from SQLite, including activity, events, agents, models, tools, costs, MCP counts, and basic graph/timeline data.
- Wired SQL-backed Overview and Sessions view models into `reflect report` through `/api/sql/overview`, `/api/sql/sessions`, and an embedded `sqlite` payload in `/api/data`.
- Added SQL-backed Overview and Sessions view models for the upcoming Textual/report migration path, including paginated session filters over canonical SQLite tables and rollups.
- Added `reflect db rebuild-rollups` to refresh session, daily, and tool aggregate tables from canonical SQLite data.
- Added `reflect db rebuild-graph` to populate SQLite graph nodes and edges from canonical sessions, steps, tools, MCP calls, and memories.
- Added `reflect db normalize` to promote pending `raw_events` into canonical sessions, steps, and LLM/tool/MCP/memory/privacy tables.
- Added local hook span JSONL ingestion via `reflect ingest --spans-file <file>` and `reflect db ingest-spans`.
- Added `reflect db doctor` to report SQLite migration drift, foreign-key violations, and runtime pragma health.
- Added SQLite canonical table migration (`004_canonical.sql`) for agents, repos, files, sessions, steps, LLM/tool/MCP calls, specs, evidence, memories, and privacy findings.
- Added SQLite rollup and graph migrations (`002_rollups.sql`, `003_graph.sql`) for session/day/tool aggregates plus graph nodes and edges.
- Added `reflect db ingest-otlp --otlp-traces <file>` to ingest OTLP traces JSON into `raw_events` with `source_id + content_hash` deduplication.
- Added `reflect.store.ingest` ingestion helper plus regression coverage for duplicate ingest behavior.
- Added `reflect db migrate` to apply bundled SQLite SQL migrations and bootstrap runtime tables from migration files.
- Added `reflect schema export --output <path>` to emit JSON Schema for the core Pydantic event model.
- Added initial `reflect.schema` + `reflect.store.migrate` foundations and regression tests for migration idempotency and schema validation behavior.
- Added initial SQLite runtime store scaffolding with a connection helper that enforces Reflect runtime pragmas (`foreign_keys`, WAL, synchronous mode, checkpoint, busy timeout) and an `optimize` helper.
- Added initial SQL migration (`001_initial.sql`) that creates `schema_migrations`, `raw_events`, and the core raw-event indexes including source/hash dedupe.
- Added regression tests that assert SQLite runtime pragma defaults and strict-durability behavior.

### Changed
- Promote the showcase page to the root landing page at `reflect.o11y.dev/`; the telemetry dashboard HTML moves to `docs/report.html`, and the deprecated `showcase.html` page is removed
- Added shorter ingest UX: `reflect ingest --otlp <file>` (kept `reflect db ingest-otlp --otlp-traces` as a legacy alias).
- Added a living SQLite/Textual execution checkpoint document (`docs/specs/reflect-sqlite-textual-checkpoint.md`) that tracks completed phases, remaining scope, and immediate next tasks toward full spec fulfillment.

### Fixed
- Point dashboard missing-report and failed-report fallbacks at `reflect.o11y.dev` instead of the showcase page
- Hide the `DEMO` badge for local `?report=api/data` dashboards
- Derive session agent filters, labels, and colors from report data without a fixed agent allowlist, including safe escaping for report-provided agent names
- Add Codex to the public showcase dashboard artifact and restore spacing between Tools summary widgets and Event Distribution
- Aligned new SQLite ingest/migration modules with Ruff rules (`datetime.UTC`, tighter exception assertions, and unused-import cleanup) so lint checks pass cleanly.
- Made top-level `reflect` package exports lazy so focused module tests can import `reflect.store.*` without importing runtime modules that require newer Python datetime APIs at import time.

## 0.7.2 (2026-05-04)

### Added
- Native OpenAI Codex CLI OTLP telemetry is now a first-class agent in reflect: Codex session, prompt, tool lifecycle, and token events from `otel-logs.json` are normalized into Reflect's hook-like span model and displayed alongside Claude, Copilot, Cursor, and Gemini
- Demo data now includes Codex OTLP log fixtures so `reflect --demo` shows Codex sessions with models, tool usage, and token counts out of the box
- Low-level Codex Rust/runtime transport spans are filtered from `otel-traces.json` to keep agent analytics clean

### Fixed
- Tuned `docs/showcase.html` typography and panel rendering to match the production reflect.o11y.dev visual baseline and improve font clarity
- Rebalanced showcase contrast ratios (navigation, body copy, cards, footer) and interactive states so tab-like navigation links are clearer and more accessible
- Rethemed the dashboard/report HTML to use the same warm reflect showcase brand palette, sharper panels, and clearer active/filter states

## 0.7.1 (2026-04-28)

### Added
- `reflect doctor` now reports LiteLLM pricing status, cache freshness, model count, and fallback details for cost-estimate troubleshooting
- Dashboard cost surfaces now show total/input/output/cache cost, model cost share, per-session cost badges, agent cost, and cost-based session sorting

### Changed
- Source development docs now use Poetry commands for install, CLI smoke tests, and pytest validation
- Demo traces now include model metadata so `reflect --demo` can show nonzero estimated cost

### Fixed
- Cost diagnostics now remain visible when token data exists but model pricing cannot be resolved
- Pricing fallback and model canonicalization now cover the demo model families used by current reflect fixtures
- Default LiteLLM pricing sync now uses the raw repository-hosted pricing JSON instead of the broken `litellm.ai` URL

## 0.7.0 (2026-04-26)

### Added
- `reflect skills` now builds a deterministic evidence bundle from session quality scores, workflow fingerprints, shell commands, recovery chains, and bounded deep session context before invoking the extraction agent
- Skill extraction prompts now ask for evidence-backed improvement rationale and provenance metadata per candidate skill instead of relying on pattern frequency alone
- Added `reflect.config` as a centralized runtime config module for resolving reflect home/config/cache/state paths and loading model alias mappings from `~/.reflect/config/model-aliases.json`
- Added LiteLLM runtime config loading (`~/.reflect/config/litellm.json`) with env-var overrides so you can point reflect pricing to your own LiteLLM endpoint / model-prices URL
- Added `reflect.pricing` foundation module with LiteLLM model-pricing table loading (live, cache, fallback), model canonicalization, and token-to-cost breakdown helpers
- Added estimated cost aggregation fields to telemetry (`total_cost_usd`, per-session costs, model cost totals, per-agent cost totals) derived from token usage + pricing table resolution
- Added `pricing_unit` support (for example `usd`, `coins`, `credits`) so cost analytics can be displayed without forcing USD-only naming

### Changed
- Moved skill-extraction helpers into `src/reflect/skill_extraction.py` and kept `reflect.core` re-exports for backward compatibility
- `reflect skills` now passes both a compact evidence summary and authoritative JSON bundle to the extraction agent
- Skill extraction docs now describe the evidence-driven workflow instead of a thin predefined prompt
- Deprecated `python serve.py` and legacy `reflect --publish` references in docs/UI copy; use `reflect report` (or `python3 -m reflect.core report`) to open the local dashboard.
- Pricing/recommendations spec now documents a dedicated central config layer for runtime config and alias loading, and removes external repository references in favor of direct LiteLLM-oriented assumptions
- Dashboard/report/terminal renderers now expose estimated cost signals and pricing provenance (`pricing_source`) alongside token usage
- Recommendations now include cost-focused signals for high total spend and single-model cost concentration
- README and hosted showcase copy now call out estimated cost analytics and document custom LiteLLM pricing endpoint configuration

### Fixed
- `reflect skills` now accepts agent output where a valid JSON array is followed by trailing prose instead of failing with `Could not parse agent output as JSON: Extra data`
- `reflect setup` no longer auto-installs the bundled `skills` helper into every detected agent's skills directory
- `reflect doctor` now trims the support matrix to implemented agents plus the planned OpenClaw and Antigravity rows, and the native telemetry panel now renders a more capability-oriented matrix (native OTel, traces, metrics, logs, config surface, protocol, status)
- Pricing fetch now validates URL schemes (`http`/`https` only), supports optional bearer auth from `api_key_env`, and prefers fresh cache reads before attempting live network fetches
- Cost aggregation now skips pricing-table loads for empty analyses and reuses a single alias map per run to avoid repeated file reads
- Showcase hero copy no longer contains the malformed leading comma in the updated telemetry/cost sentence

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
