# Changelog

## 0.9.1 (unreleased)

### Fixed

- Replaced the outdated social preview artwork with the current reflect visual identity and ensured the Open Graph image ships with the packaged dashboard.
- Updated the public landing-page release fallback to the current package version.
- Added safe canvas padding to the shared logo mark so README and favicon renderers do not crop its edges.

### Changed

- Removed archived implementation-plan and migration-spec snapshots from the public documentation tree.

## 0.9.0 (2026-07-22)

### Added

- Added instant multi-word free-text search to the Skills registry across names, descriptions, lifecycle, provenance, source agents, availability, and installation targets, with URL-persisted filtering and visible result counts.
- Added first-class ingestion for `opentelemetry-hooks` fact contract v1, including privacy-safe conversation facts, stable hook event identity, provider/schema provenance, native trace links, and subagent parent relationships.
- Added queryable `conversation_facts` and `agent_events` ledgers plus session telemetry contract summaries and agent-ID-aware delegation graph edges.
- Replaced the private `reflect-mcp` JSON-lines dispatcher with a standards-compliant FastMCP stdio server exposing read-only context, improvement, provenance, and exact-usage tools; `reflect ask` now uses the same context service and returns scoped memory with explicit provenance.
- Added an optional OMEGA Memory provider using OMEGA's public local `SQLiteStore` API for health, semantic search, mirrored writes, inspection, deletion, and validation, with explicit generic-session routing and local SQLite fallback.
- Added exact current-session, selected-session, and uncapped global usage reporting through `reflect usage`, with token, cost, model, tool, MCP, subagent, duration, and failure breakdowns plus a globally distributed `$reflect-usage` helper skill.
- Added Click-native Bash, Zsh, and Fish autocomplete across the full command tree, including idempotent installation and privacy-safe local ID suggestions for observations, workflows, loops, sessions, skills, and memories.
- Added an adapter-neutral conversation reader with readable and full-activity modes, in-session search and result navigation, synchronized timeline jumps, failure navigation, turn labels, and prompt/response copy actions.
- Added a typed native-session conversation adapter registry for Claude, Codex, Copilot, Cursor, and Gemini, including common CLI-name aliases and a narrow extension contract for additional agent formats.
- Added an accessible bidirectional session conversation playhead with draggable event snapping, keyboard navigation, compact event context, focused conversation highlighting, and timeline updates while manually scrolling the thread.
- Added object-oriented Session Rules with typed definitions, normalized telemetry and summary contexts, a validated registry, a shared scorer, and a documented extension path for per-session quality dimensions.
- Added a public object-oriented `BaseImprovementRule`, typed observation builder, validated `RuleRegistry`, and documented custom-rule extension path.
- Added typed, rule-owned `WorkflowDefinition` proposals so custom and built-in rules explicitly opt into workflow creation while observation-only rules remain supported.
- Added the SQLite-backed Improvement Ledger with versioned rules, durable observations, redacted evidence links, session outcomes, workflow candidates and versions, interventions, measurements, feedback, bounded nudges, and signed team-bundle imports.
- Added all ten deterministic P0 detector families, including explicit correction and correct-no-change outcomes, enforced-boundary violations, successful recovery sequences, and repeated high-performing routines.
- Added the simplified `reflect improve`, `reflect ask`, `reflect loops`, `reflect skills`, first-class `reflect workflows`, and `reflect feedback` command contracts.
- Added Recovery, Verification, Exploration, and Proven Pattern workflow behavior types with CLI and browser filtering, while retaining Loop as compatibility metadata for imported historical candidates.
- Added exact workflow diff previews, audited candidate editing and rejection, idempotent hash-guarded application, safe rollback under `.agents/skills/`, automatic conservative before/after measurements, and aggregate-only signed team bundles.
- Added deterministic task archetypes, task-scoped measurement cohorts, workflow exposure/adherence states, and rendered-artifact integrity evaluations.
- Added browser APIs and controls for evidence drill-down, workflow review/edit/apply/reject/rollback, session outcome feedback, and regression review.
- Added a browser-visible detector registry plus source-session and post-activation workflow session ledgers.
- Added a disabled, metadata-only nudge filesystem contract with private permissions, hashed session routing, and atomic JSON writes for a future `opentelemetry-hooks` reader; current setup does not configure or poll it.
- Added canonical workspace identity, optional local Git repository resolution, historical session-context backfill, and parent/child session lineage for the Behavioral Memory Graph.
- Added a first-class `reflect loops [show|build]` behavior ledger for stalled retries and productive repeated routines, with bounded source-session evidence and deliberate promotion into one pending workflow packaged as a skill.
- Added the Skills v2 SQLite registry with stable skill identities, immutable content versions, evidence provenance, installation reconciliation, telemetry-observed usage, measurement storage, and `reflect skills [discover|show|apply|rollback]` commands.
- Added browser APIs and dedicated product surfaces for observed loops, reusable workflows, and the durable skill registry.
- Added typed workflow source and suggested-artifact contracts so deterministic rule blueprints, coding-agent drafts, and imported skills retain distinct provenance.

### Changed

- Added a responsive, task-oriented landing-page scenario grid for usage and cost, session diagnosis, recurring friction, loop-to-skill authoring, repository guidance, memory recall, and instrumentation health.
- Updated the public landing page and README to explain agent-neutral hook/native correlation, the searchable Skills registry, Reflect's read-only MCP, and every supported memory provider with accurate connected-versus-discovery-only capability labels.
- Promoted explicit hook workspace, repository owner/name, branch, and credential-free remote hashes into canonical workspace and repository identity, including upgrades of existing stored steps.
- Rebuilt the README around the "Evidence, Not Vibes." product promise, a short verified setup-to-dashboard quick start, the current evidence-to-improvement product model, local privacy defaults, and accurate cross-agent skill distribution guidance.
- Redesigned the public landing page around a clear capture, understand, and improve journey with current showcase evidence and priced-cost coverage, a branded agent rail, a progressively enhanced GitHub release/star/fork proof strip, a product capability grid, local-first trust details, a focused install path, stronger responsive behavior, and accessible navigation and motion states.
- Redesigned Workflows and Skills as responsive tile grids with clearer status, provenance, evidence, usage, installation, and impact hierarchy; workflow steps now render as numbered readable tiles in both cards and review dialogs.
- Distinguished Codex-visible, workspace-scoped, other-agent, pending, telemetry-only, archived, and uninstalled skill records in the Skills registry and review dialog.
- Marked imported skill identities stale when their final active filesystem installation disappears, while preserving their version and installation history and reactivating them if the package returns.
- Removed the replaced conversation-card renderer, telemetry beat rail, stale chart helper, and their unused styles and event handlers from the browser asset.
- Aligned browser navigation, URL state, DOM identifiers, and frontend requests around the product-language Inbox, Impact, and Explore contracts; Explore now uses explicit Usage, Tools, Graph, and Context views, with legacy URLs and API routes retained only as compatibility aliases.
- Moved direct session comparison into Sessions and generic cohort analysis into Explore → Usage so Impact is reserved for measured post-application outcomes.
- Reframed the Impact view to group measurement snapshots by applied workflow, show evidence-collection progress before making claims, hide premature deltas and confidence, retain history under disclosure, and link each result to stored or explicitly reconstructed comparison-session cohorts.
- Redesigned observation-evidence and workflow-review dialogs around compact decision briefs, professional section hierarchy, human-readable session rows, bounded evidence disclosure, and anchored review actions.
- Restructured the browser report around Inbox, Sessions, Workflows, Skills, Impact, and Explore while preserving the existing usage, cost, comparison, tools, graph, context, memory, privacy, export, and achievement widgets.
- Moved observed loops into the evidence-first Inbox, gave Workflows an independent review and delivery surface, and limited Skills to durable package versions, installations, usage, provenance, and measurements.
- Removed legacy telemetry summaries, prompt examples, and rule administration from Inbox; Improvement Rules now live under Explore → Context & system.
- Restructured session detail into Summary, Conversation, Execution, Changes, and Evidence views.
- Changed `reflect skills` to reconcile and list the durable registry by default; agent-assisted extraction is explicit under `reflect skills discover`, and generated skills remain pending until operator approval.
- Made deterministic, versioned SQLite rules the durable observation source while retaining the existing telemetry insight analysis as supporting context.
- Changed the packaged Reflect skill to query approved `reflect ask` guidance before recurring work and to keep setup/configuration mutation explicitly operator-authorized.
- Changed workflow review to lead with a readable change summary, repository target, proposed steps, abstention criteria, verification, and session provenance while keeping raw file diff and JSON editing under advanced disclosure.
- Clarified workflow repository selection as the repo-local skill application destination and replaced ambiguous lock language with explicit active-target ownership and rollback semantics.
- Tightened deterministic workflow discovery by requiring repeated failures across multiple sessions, using non-saturating failure impact scoring, and rejecting repeated single-tool sequences as reusable workflows.
- Changed graph Folder and Path identity to workspace-relative shared nodes, kept activity on session-scoped weighted edges, and added Same workspace versus Selected session browser views with on-demand complete workspace expansion and cross-agent session labels.
- Grouped evidence-specific workflow rows into one versioned reusable skill per slug, with unique supporting sessions, evidence-pattern counts, and scopes retained behind the skill.
- Separated loops, workflows, and skills: loops represent observed repeated behavior, workflows represent reusable reviewable procedures, and skills represent durable installable packages produced by the current workflow renderer.
- Removed the bundled `reflect-loops` helper skill because the real `reflect loops` command now owns detection, evidence review, and selected-loop skill generation.
- Labeled workflow suggestions as Rule blueprint, Agent-authored draft, or Imported skill in the CLI and browser, and recorded the selected extraction agent on new agent-authored drafts.
- Extended `reflect workflows add` with optional source-agent and source-workflow provenance so agent-authored loop skills retain their author and bounded source sessions without adding another top-level command.

### Dependencies

- Added the stable official MCP Python SDK (`mcp>=1.28,<2`) for protocol lifecycle, schemas, stdio transport, and MCP client compatibility while avoiding the v2 prerelease line.

### Fixed

- Marked Windsurf hook telemetry as implemented in setup and doctor, wired `reflect setup --agent windsurf` through `otel-hook setup --global --agent windsurf`, and aligned the support matrix documentation.

- Added an agent-neutral, strategy-based MCP classifier that normalizes standard attributes, encoded tool names, and payload-based calls from all supported agents into the canonical MCP ledger; refresh now repairs existing sessions while preferring native spans over duplicate hook or transcript evidence.
- Declared the `src/reflect` package explicitly for Poetry so the documented source-development install completes instead of stopping after dependency installation.
- Updated Codex skill distribution to use the current user-wide and project-local `.agents/skills/` discovery roots, so the packaged `reflect-skills` helper appears in Codex's skill and slash-command lists after setup.
- Kept workflow candidates addressable and grouped across paginated ledgers larger than 500 records instead of silently hiding later candidates and variants.
- Refreshed impact snapshots when sessions or metric values change inside a full 50-session cohort, even when the before/after counts remain constant.
- Merged native prompt and response content into telemetry-backed conversations without dropping MCP calls, tool status, duration, or other execution evidence.
- Made optional shell-autocomplete installation during setup best-effort so shell configuration permission errors do not invalidate completed telemetry setup.
- Restored every Explore → Usage widget under agent, model, status, range, search, and session filters: bounded usage-specific aggregates now keep tool metrics, failure rates, source provenance, weekly activity, scoped cost trends, subagent completion, and explicit zero-data chart states on the same filtered session cohort; command summaries also redact credential-like values.
- Preserved assistant response text from native Claude, Codex, Copilot, Cursor, and Gemini sessions, preferred those high-fidelity transcripts in SQL-backed session detail, and retained native source provenance when telemetry and local-session records merge.
- Fixed Conversation preview expansion so it reveals the stored response or prompt body, persists across detail rerenders, and is not overridden after full session detail loads.
- Migrated Codex native telemetry setup to the current `exporter` and `trace_exporter` schema while preserving user-owned OTel settings, and made doctor report prompt-content capture truthfully.
- Prevented setup tests and stale daemons from sharing the local OTLP ports, added gateway ownership metadata, startup readiness checks, and sandbox-safe PID probing, and surfaced unmanaged listeners with their actual trace destination.
- Kept lazy Graph loading responsive on large stores by deriving its tool and MCP summaries from bounded aggregate queries instead of rescanning skill, command, duration, and file telemetry that the graph does not render.
- Reduced filtered Explore → Usage cohort analysis to the bounded tool, shell, MCP, subagent, and agent aggregates it renders instead of rebuilding every dashboard tab for both cohorts.
- Populated prompt summaries for every session-sidebar navigation card and stopped token-bearing LLM generations from appearing as synthetic metadata-only user prompts in Conversation.
- Prevented session-filtered dashboards from remaining on the loading screen by keeping improvement GET endpoints read-only and bounding non-critical improvement-data startup requests.
- Displayed telemetry-observed skill-use sessions in Skills review, separately from the source sessions that produced generated skills.
- Grouped scope- and tool-specific observation rows into durable Inbox findings, excluded archived telemetry-only skill identities from the default Skills surface, and replaced page-length badge counts with explicit API totals.
- Reduced loop false positives by requiring consecutive same-input runs, excluding approval and wait/poll transport events, requiring cross-session recurrence for failure-free patterns, hiding resolved loops by default, and replacing expensive windowed detection with a bounded streaming pass.
- Prevented evidence-specific workflow rows, tool-specific trigger descriptions, and volatile observation IDs from appearing as duplicate skill versions; Skills v2 now reports semantic workflow versions with distinct evidence while retaining the full provenance ledger.
- Kept intrinsic-width dashboard tables and heatmaps inside the report content grid so Subagent Effectiveness no longer overflows beneath the session filter rail.
- Removed the duplicate SQLite dashboard quality rubric and scoring formulas so telemetry and summary-backed session detail use the same registered Session Rules implementation.
- Marked pending workflow candidates stale when their source observation resolves, and reopened them if the same evidence-backed finding returns.
- Preserved observation identity and review state across refreshes, resolved vanished findings instead of deleting them, and kept generated evidence redacted and traceable to canonical entities.
- Prevented workflow rollback from overwriting operator edits made after Reflect applied a generated skill.
- Prevented repeated workflow application from creating duplicate active interventions, detected missing or modified applied artifacts as stale, and avoided duplicate measurements when cohort sizes have not changed.
- Prevented browser workflow application outside a selected Git repository and blocked different active candidates from writing the same workflow target.
- Populated privacy-safe tool input/output hashes during normalization and backfilled hashes from existing redacted previews so identical-input retry loops produce observations and workflow candidates.
- Bounded retry-loop detection to one grouped canonical query and added a partial tool-input fingerprint index so large local stores refresh without one full scan per detected tool.
- Fixed disconnected same-folder sessions, discarded repeated-edge strength, stale derived graph state, missing Cursor child-session links, and preview parsing that produced folder noise such as glob or prose fragments.
- Serialized first-time SQLite WAL initialization and pending migrations across concurrent dashboard requests so background preparation and session-detail reads cannot race on database setup, while fully migrated requests use a read-only fast path that does not contend with graph rebuilds.
- Made workflow source-session navigation use direct session links that clear conflicting workflow and filter state while preserving the focused evidence ID.
- Kept grouped workflow evidence-pattern counts aligned with the same current, non-rejected evidence set used by the source-session ledger.

## 0.8.7 (2026-07-13)

### Added
- Added `REFLECT_DEBUG_PERF=1` route timing logs for dashboard data and lazy tab API requests.
- Added SQLite hot-path indexes for session-scoped dashboard tab, graph, detail, and export queries.
- Added persisted source fingerprints so repeated report preparation skips unchanged telemetry files.
- Added an observable background report-preparation worker and `/api/status` lifecycle endpoint.
- Added a managed background browser-report server with `reflect server start`, `stop`, and `status` commands plus a `--foreground` debugging mode.

### Fixed

- Keep filtered dashboard requests responsive during background ingestion by preserving concurrent WAL reads and deferring heavy tab queries.
- Added a focused SQLite fast path for single-session dashboard filters so `/api/data?session=<id>` avoids broad session payload and tab graph builders.
- Added SQL-backed lazy tab endpoints for dashboard graphs, data, memory, privacy, specs, and exports so session-scoped heavy panels can load on demand.
- Reused current cost, graph, and rollup state when report preparation produces no canonical telemetry changes.
- Scoped repricing and aggregate rollup refreshes to sessions changed during telemetry normalization.
- Scoped graph normalization to changed sessions while reusing the canonical graph builder.
- Served existing SQLite report snapshots immediately while telemetry refresh runs in the background.
- Preserved the full session navigation when opening a selected session while keeping dashboard calculations scoped to that session.
- Kept Tools skill inventory and the other operational tabs consistent with session detail data, and added visible loading states for lazy Graphs and Data tabs.
- Made bare `reflect` start or reuse the browser report in the background so the terminal returns immediately with the dashboard URL and management commands.
- Added managed, unmanaged-listener, and stopped browser report server status to `reflect doctor`.

## 0.8.6 (2026-07-07)

### Added
- Added a SQLite-first Reflect Memory provider subsystem with evidence validation, folder-scoped sync/list/search/inspect/forget/validate commands, provider discovery, graph-derived memory candidates, and local MCP-style memory tool handlers.
- Added additive SQLite memory provider metadata, memory FTS search, and graph-derived memory candidate storage.
- Added optional LiteLLM Proxy `/v1/memory` and Memory Palace memory providers, with local SQLite mirroring and provider health discovery.

### Changed
- Moved local instruction-memory sync from `reflect db sync-instructions` to `reflect memory sync [PATH]`.
- Removed deprecated `reflect report`, `--terminal`, `--no-terminal`, and `--sql-only` command surfaces; `reflect` is now the browser report entry point.
- Updated docs and bundled dashboard copy to show the Behavioral Memory Graph and local memory workflow.

### Fixed
- Fixed `reflect setup --agent codex` and related setup aliases so Codex skill distribution targets `~/.codex/skills/`.

## 0.8.5 (2026-06-28)

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
